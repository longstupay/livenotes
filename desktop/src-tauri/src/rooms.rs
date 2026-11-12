//! 房间 WebView2 子视图、采集注入、PCM 回传与静音。
//!
//! 传输路线（docs/20 4.4 的三选一）这里选的是 WebView2 原生 web message：
//! 页面里的注入脚本只调用 `window.chrome.webview.postMessage`，不 fetch 本地端口，
//! 因此不受直播页 CSP / CORS 影响；壳在 WebMessageReceived 里统一做房间、大小、
//! 频率校验，再转发给 Engine `/api/live/chunk`。
//!
//! 这比给远程页面开 Tauri IPC capability 更容易验证，也把「页面能做什么」
//! 收敛到了一个只写消息的通道上。

use std::collections::HashMap;
use std::sync::mpsc::{Receiver, Sender};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use base64::Engine as _;
use serde::{Deserialize, Serialize};
use serde_json::json;
use tauri::{
    webview::NewWindowResponse, AppHandle, LogicalPosition, LogicalSize, Manager, State, Webview,
    WebviewUrl, WebviewWindow, WebviewWindowBuilder, Window,
};

use crate::diag;
use crate::engine;
use crate::http;

pub const ADDITIONAL_ARGS: &str = concat!(
    "--disable-features=msWebOOUI,msPdfOOUI,msSmartScreenProtection ",
    "--autoplay-policy=no-user-gesture-required ",
    "--disable-background-timer-throttling ",
    "--disable-renderer-backgrounding ",
    "--disable-backgrounding-occluded-windows"
);

#[derive(Clone)]
pub struct ChunkJob {
    pub room: String,
    pub raw: String,
}

#[derive(Default, Clone)]
pub struct RoomStat {
    pub sid: String,
    pub chunks: u64,
    pub bytes: u64,
    pub drops: u64,
    pub last: Option<Instant>,
}

/// 主窗口的 window.open 交给应用内浏览器时，先放进这个队列。
/// 浏览器页加载/轮询时取走，避免在页面还没就绪时 eval 丢失。
#[derive(Clone, Serialize, Deserialize)]
pub struct PendingTab {
    pub url: String,
    pub capture: bool,
    /// 采集会话 id：桌面自己接住房间时由壳先建好，浏览器页不再重复建。
    #[serde(default)]
    pub sid: String,
    /// 对应的引擎房间 id；空 = 普通工具页/手动标签页。
    #[serde(default)]
    pub room: String,
    /// "open"（默认）或 "close"（房间被飞书 /stop、收听页停止后关标签页）。
    #[serde(default)]
    pub action: String,
}

pub struct RoomState {
    pub port: u16,
    pub tx: Sender<ChunkJob>,
    pub rooms: Mutex<HashMap<String, Webview>>,
    pub sessions: Arc<Mutex<HashMap<String, String>>>,
    pub stats: Arc<Mutex<HashMap<String, RoomStat>>>,
    /// 主动关闭房间时先登记，避免 WebView2 销毁过程被误报成“房间视图崩溃”。
    pub closing: Arc<Mutex<HashMap<String, Instant>>>,
    pub pending: Arc<Mutex<Vec<PendingTab>>>,
    /// 标签页 label -> 引擎房间 id，关窗时用来把房间一起收掉。
    pub tab_rooms: Arc<Mutex<HashMap<String, String>>>,
}

#[tauri::command]
pub async fn room_open(
    window: Window,
    app: AppHandle,
    state: State<'_, RoomState>,
    label: String,
    url: String,
    sid: String,
    capture: Option<bool>,
    room: Option<String>,
    x: f64,
    y: f64,
    w: f64,
    h: f64,
) -> Result<(), String> {
    let capture = capture.unwrap_or(true);
    let parsed: tauri::Url = url
        .parse()
        .map_err(|e| format!("invalid room url {url:?}: {e}"))?;
    if !matches!(parsed.scheme(), "http" | "https") {
        return Err(format!("room url must be http(s), got {}", parsed.scheme()));
    }

    if let Some(old) = state.rooms.lock().unwrap().remove(&label) {
        let _ = old.close();
    }
    // 上一次关闭留下的“预期关闭”标记不能压住这次重开后的真实崩溃。
    state.closing.lock().unwrap().remove(&label);

    let log_path = engine::app_data_root()
        .ok()
        .map(|p| p.join("logs").join("shell.log"));
    if let Some(path) = &log_path {
        engine::append_log(
            path,
            &format!("room_open label={label} url={url} sid={sid} capture={capture}"),
        );
    }

    let profile = engine::webview_dir("rooms")?;

    let builder = tauri::webview::WebviewBuilder::new(&label, WebviewUrl::External(parsed))
        .data_directory(profile)
        .additional_browser_args(ADDITIONAL_ARGS)
        .on_navigation(|u| matches!(u.scheme(), "http" | "https"));
    // 工具页（收听/文件中心等）不注入采集脚本，也没有会话；它里面的
    // window.open 继续作为新的工具页标签打开，而不是静默失败。
    let builder = if capture {
        builder.initialization_script(capture_script(&label))
    } else {
        let app = app.clone();
        builder.on_new_window(move |url, _| {
            if matches!(url.scheme(), "http" | "https") {
                browser_enqueue(&app, url.as_str(), false);
            }
            NewWindowResponse::Deny
        })
    };

    let webview = window
        .add_child(
            builder,
            LogicalPosition::new(x, y),
            LogicalSize::new(w, h),
        )
        .map_err(|e| format!("add child webview: {e}"))?;

    if capture {
        attach_message_handler(&webview, &label, state.tx.clone())?;
        // 视图进程崩溃是“页面看起来还在、字幕却不动了”的典型原因；注册 COM
        // ProcessFailed 回调比等 120 秒断流看门狗快得多。失败不影响房间打开。
        if let Err(e) = attach_crash_handler(
            &webview,
            &label,
            app,
            state.closing.clone(),
        ) {
            if let Some(path) = &log_path {
                engine::append_log(path, &format!("attach_crash_handler {label} failed: {e}"));
            }
        }
    }
    if let Some(path) = &log_path {
        engine::append_log(path, &format!("room_open ok label={label}"));
    }

    if capture {
        state
            .sessions
            .lock()
            .unwrap()
            .insert(label.clone(), sid.clone());
        state.stats.lock().unwrap().insert(
            label.clone(),
            RoomStat {
                sid,
                ..Default::default()
            },
        );
    }
    if let Some(rid) = room.filter(|r| !r.is_empty()) {
        state
            .tab_rooms
            .lock()
            .unwrap()
            .insert(label.clone(), rid);
    }
    state.rooms.lock().unwrap().insert(label, webview);
    Ok(())
}

#[tauri::command]
pub fn room_close(state: State<'_, RoomState>, label: String) -> Result<(), String> {
    state
        .closing
        .lock()
        .unwrap()
        .insert(label.clone(), Instant::now());
    let webview = state.rooms.lock().unwrap().remove(&label);
    state.sessions.lock().unwrap().remove(&label);
    state.stats.lock().unwrap().remove(&label);
    state.tab_rooms.lock().unwrap().remove(&label);
    if let Some(webview) = webview {
        webview.close().map_err(|e| e.to_string())?;
    }
    Ok(())
}

#[tauri::command]
pub fn room_layout(
    state: State<'_, RoomState>,
    label: String,
    x: f64,
    y: f64,
    w: f64,
    h: f64,
) -> Result<(), String> {
    let webview = state.rooms.lock().unwrap().get(&label).cloned();
    if let Some(webview) = webview {
        webview
            .set_position(LogicalPosition::new(x, y))
            .map_err(|e| e.to_string())?;
        webview
            .set_size(LogicalSize::new(w, h))
            .map_err(|e| e.to_string())?;
    }
    Ok(())
}

#[tauri::command]
pub fn room_show(state: State<'_, RoomState>, label: String, visible: bool) -> Result<(), String> {
    let webview = state
        .rooms
        .lock()
        .unwrap()
        .get(&label)
        .cloned()
        .ok_or_else(|| format!("room {label} is not open"))?;
    if visible {
        webview.show()
    } else {
        webview.hide()
    }
    .map_err(|e| e.to_string())
}

#[tauri::command]
pub fn room_navigate(
    state: State<'_, RoomState>,
    label: String,
    url: String,
) -> Result<(), String> {
    let parsed: tauri::Url = url
        .parse()
        .map_err(|e| format!("invalid room url {url:?}: {e}"))?;
    if !matches!(parsed.scheme(), "http" | "https") {
        return Err(format!("room url must be http(s), got {}", parsed.scheme()));
    }
    let webview = state
        .rooms
        .lock()
        .unwrap()
        .get(&label)
        .cloned()
        .ok_or_else(|| format!("room {label} is not open"))?;
    webview.navigate(parsed).map_err(|e| e.to_string())
}

#[tauri::command]
pub fn room_reload(state: State<'_, RoomState>, label: String) -> Result<(), String> {
    let webview = state
        .rooms
        .lock()
        .unwrap()
        .get(&label)
        .cloned()
        .ok_or_else(|| format!("room {label} is not open"))?;
    webview
        .eval("location.reload()")
        .map_err(|e| e.to_string())
}

#[tauri::command]
pub fn room_history(
    state: State<'_, RoomState>,
    label: String,
    delta: i32,
) -> Result<(), String> {
    let delta = delta.clamp(-10, 10);
    let webview = state
        .rooms
        .lock()
        .unwrap()
        .get(&label)
        .cloned()
        .ok_or_else(|| format!("room {label} is not open"))?;
    webview
        .eval(format!("history.go({delta})"))
        .map_err(|e| e.to_string())
}

#[tauri::command]
pub fn room_url(state: State<'_, RoomState>, label: String) -> Result<String, String> {
    let webview = state
        .rooms
        .lock()
        .unwrap()
        .get(&label)
        .cloned()
        .ok_or_else(|| format!("room {label} is not open"))?;
    webview
        .url()
        .map(|u| u.to_string())
        .map_err(|e| e.to_string())
}

#[tauri::command]
pub fn room_focus(state: State<'_, RoomState>, label: String) -> Result<(), String> {
    let webview = state
        .rooms
        .lock()
        .unwrap()
        .get(&label)
        .cloned()
        .ok_or_else(|| format!("room {label} is not open"))?;
    webview.set_focus().map_err(|e| e.to_string())
}

#[tauri::command]
pub fn browser_take_pending(state: State<'_, RoomState>) -> Vec<PendingTab> {
    std::mem::take(&mut *state.pending.lock().unwrap())
}

#[tauri::command]
pub fn room_mute(state: State<'_, RoomState>, label: String, muted: bool) -> Result<(), String> {
    let webview = state
        .rooms
        .lock()
        .unwrap()
        .get(&label)
        .cloned()
        .ok_or_else(|| format!("room {label} is not open"))?;
    let log_path = engine::app_data_root()
        .ok()
        .map(|p| p.join("logs").join("shell.log"));
    let log_label = label.clone();
    webview
        .with_webview(move |pw| {
            #[cfg(windows)]
            unsafe {
                use webview2_com::Microsoft::Web::WebView2::Win32::ICoreWebView2_8;
                use windows::core::Interface as _;

                // 静音在 CoreWebView2_8 上；基础 ICoreWebView2Controller 没有 SetIsMuted。
                let mut outcome = String::from("no core");
                if let Ok(core) = pw.controller().CoreWebView2() {
                    match core.cast::<ICoreWebView2_8>() {
                        Ok(core8) => {
                            let set = core8.SetIsMuted(muted);
                            let mut read = windows::core::BOOL::default();
                            let got = core8.IsMuted(&mut read);
                            outcome = format!(
                                "set={set:?} read={got:?} is_muted={}",
                                read.as_bool()
                            );
                        }
                        Err(e) => outcome = format!("cast failed: {e}"),
                    }
                }
                if let Some(path) = &log_path {
                    engine::append_log(
                        path,
                        &format!("room_mute {log_label} muted={muted} {outcome}"),
                    );
                }
            }
        })
        .map_err(|e| e.to_string())
}

#[tauri::command]
pub fn live_start(state: State<'_, RoomState>, title: String) -> Result<String, String> {
    let body = json!({"title": title, "source_mode": "tab"});
    let resp = http::post_json(
        state.port,
        "/api/live/start",
        &body,
        Duration::from_secs(20),
    )?;
    if !(200..300).contains(&resp.status) {
        return Err(format!("live/start HTTP {}: {}", resp.status, resp.text()));
    }
    let value = resp.json()?;
    value
        .get("session_id")
        .and_then(|v| v.as_str())
        .map(|s| s.to_string())
        .ok_or_else(|| format!("live/start returned no session_id: {value}"))
}

#[tauri::command]
pub fn live_stop(state: State<'_, RoomState>, sid: String) -> Result<(), String> {
    let body = json!({"session_id": sid});
    let resp = http::post_json(state.port, "/api/live/stop", &body, Duration::from_secs(30))?;
    if resp.status != 200 {
        return Err(format!("live/stop HTTP {}: {}", resp.status, resp.text()));
    }
    Ok(())
}

/// 打开实时字幕窗口。
///
/// 必须是 async 命令：同步命令在 Tauri 里跑在事件循环线程上，而 Windows 上建
/// WebView2 窗口要等控制器异步创建完成；在事件循环线程里同步 `build()` 会把
/// 消息泵堵死 —— 窗口出来了但一直是白屏，整个壳也关不掉。async 让窗口创建从
/// 线程池发起，由壳在主线程分发完成（`room_open` 同理）。
#[tauri::command]
pub async fn live_open_view(
    app: AppHandle,
    state: State<'_, RoomState>,
    sid: String,
) -> Result<(), String> {
    let sid = sid.trim().to_string();
    if sid.is_empty() {
        return Err("没有会话 id，先等这一路开始出字".into());
    }
    let label = format!("live-{sid}");
    if let Some(window) = app.get_webview_window(&label) {
        let _ = window.show();
        let _ = window.unminimize();
        let _ = window.set_focus();
        return Ok(());
    }
    let url: tauri::Url = format!("http://127.0.0.1:{}/live?session={}", state.port, sid)
        .parse()
        .map_err(|e| format!("invalid live url: {e}"))?;
    watcher_log(&format!("live_open_view label={label} url={url}"));
    let built = WebviewWindowBuilder::new(&app, &label, WebviewUrl::External(url))
        .data_directory(engine::webview_dir("live")?)
        .title("LiveNotes · 实时字幕")
        .inner_size(1280.0, 860.0)
        .min_inner_size(720.0, 480.0)
        .build();
    match built {
        Ok(window) => {
            #[cfg(windows)]
            crate::tame_webview(&window);
            watcher_log(&format!("live_open_view ok label={label}"));
            Ok(())
        }
        Err(e) => {
            watcher_log(&format!("live_open_view failed label={label}: {e}"));
            Err(e.to_string())
        }
    }
}

#[tauri::command]
pub fn live_probe(state: State<'_, RoomState>, sid: String) -> Result<serde_json::Value, String> {
    let status = http::get(
        state.port,
        &format!("/api/live/status?id={sid}"),
        Duration::from_secs(3),
    )?;
    let utts = http::get(
        state.port,
        &format!("/api/live/utterances?id={sid}&limit=5"),
        Duration::from_secs(3),
    )?;
    Ok(json!({
        "status": status.json().unwrap_or_else(|_| json!({"http": status.status, "text": status.text()})),
        "utterances": utts.json().unwrap_or_else(|_| json!({"http": utts.status, "text": utts.text()})),
    }))
}

#[derive(Serialize)]
pub struct StatView {
    pub room: String,
    pub sid: String,
    pub chunks: u64,
    pub bytes: u64,
    pub drops: u64,
    pub last_ms_ago: Option<u64>,
}

#[tauri::command]
pub fn room_stats(state: State<'_, RoomState>) -> Vec<StatView> {
    let now = Instant::now();
    state
        .stats
        .lock()
        .unwrap()
        .iter()
        .map(|(room, s)| StatView {
            room: room.clone(),
            sid: s.sid.clone(),
            chunks: s.chunks,
            bytes: s.bytes,
            drops: s.drops,
            last_ms_ago: s.last.map(|t| now.duration_since(t).as_millis() as u64),
        })
        .collect()
}

/// 验证用：用 CDP `Page.crash` 让指定房间的渲染进程真的崩一次。
/// 只允许在设置 `LIVENOTES_SPIKE` 的自动化运行时调用，正常构建不会走到。
pub fn crash_room_for_test(app: &AppHandle, label: &str) -> Result<(), String> {
    if std::env::var("LIVENOTES_SPIKE").is_err() {
        return Err("仅限 LIVENOTES_SPIKE 自动化".into());
    }
    let state = app
        .try_state::<RoomState>()
        .ok_or_else(|| "RoomState 尚未初始化".to_string())?;
    let webview = state
        .rooms
        .lock()
        .unwrap()
        .get(label)
        .cloned()
        .ok_or_else(|| format!("房间 {label} 没有打开的视图"))?;
    let (tx, rx) = std::sync::mpsc::channel();
    webview
        .with_webview(move |pw| {
            #[cfg(windows)]
            unsafe {
                use webview2_com::CallDevToolsProtocolMethodCompletedHandler;
                use windows::core::HSTRING;

                let result = (|| -> Result<(), String> {
                    let core = pw
                        .controller()
                        .CoreWebView2()
                        .map_err(|e| format!("CoreWebView2: {e}"))?;
                    let done = CallDevToolsProtocolMethodCompletedHandler::create(Box::new(
                        |_hr, _json| Ok(()),
                    ));
                    let method = HSTRING::from("Page.crash");
                    let params = HSTRING::from("{}");
                    core.CallDevToolsProtocolMethod(&method, &params, &done)
                        .map_err(|e| format!("Page.crash: {e}"))
                })();
                let _ = tx.send(result);
            }
            #[cfg(not(windows))]
            {
                let _ = (pw, tx);
            }
        })
        .map_err(|e| e.to_string())?;
    rx.recv_timeout(Duration::from_secs(5))
        .map_err(|e| format!("等待 CDP 调用: {e}"))?
}

pub fn open_rooms_console(app: &AppHandle) -> Option<WebviewWindow> {
    if let Some(window) = app.get_webview_window("rooms") {
        let _ = window.show();
        let _ = window.set_focus();
        return Some(window);
    }
    let dir = match engine::webview_dir("console") {
        Ok(d) => d,
        Err(e) => {
            engine::append_log(
                &engine::app_data_root()
                    .unwrap_or_default()
                    .join("logs")
                    .join("shell.log"),
                &format!("rooms console data dir failed: {e}"),
            );
            return None;
        }
    };
    match WebviewWindowBuilder::new(
        app,
        "rooms",
        WebviewUrl::App("rooms.html".into()),
    )
    .data_directory(dir)
    .title("LiveNotes · 多房间采集 Spike")
    .inner_size(1320.0, 880.0)
    .min_inner_size(980.0, 640.0)
    .build()
    {
        Ok(window) => Some(window),
        Err(e) => {
            engine::append_log(
                &engine::app_data_root()
                    .unwrap_or_default()
                    .join("logs")
                    .join("shell.log"),
                &format!("open rooms console failed: {e}"),
            );
            None
        }
    }
}

/// 正式的「应用内浏览器」窗口：标签栏 + 地址栏 + 子 WebView2 视图。
/// 与 spike 控制台分开，避免动到 D2/D3 的验收路径。
pub fn open_browser_console(app: &AppHandle) -> Option<WebviewWindow> {
    open_browser_console_ex(app, true)
}

/// `show=false` 时窗口只作为后台采集宿主：不弹窗、不抢焦点，
/// 但仍禁用 Chromium 的后台节流，保证采集页的定时器和音频回调照常跑。
pub fn open_browser_console_ex(app: &AppHandle, show: bool) -> Option<WebviewWindow> {
    if let Some(window) = app.get_webview_window("browser") {
        if show {
            let _ = window.show();
            let _ = window.unminimize();
            let _ = window.set_focus();
        }
        return Some(window);
    }
    let dir = match engine::webview_dir("browser-console") {
        Ok(d) => d,
        Err(e) => {
            engine::append_log(
                &engine::app_data_root()
                    .unwrap_or_default()
                    .join("logs")
                    .join("shell.log"),
                &format!("browser console data dir failed: {e}"),
            );
            return None;
        }
    };
    match WebviewWindowBuilder::new(app, "browser", WebviewUrl::App("browser.html".into()))
        .data_directory(dir)
        .title("LiveNotes · 应用内浏览器")
        .inner_size(1360.0, 900.0)
        .min_inner_size(1000.0, 640.0)
        .visible(show)
        .additional_browser_args(
            "--disable-background-timer-throttling \
             --disable-renderer-backgrounding \
             --disable-backgrounding-occluded-windows \
             --disable-features=CalculateNativeWinOcclusion \
             --autoplay-policy=no-user-gesture-required",
        )
        .build()
    {
        Ok(window) => {
            #[cfg(windows)]
            crate::tame_webview(&window);
            let app = app.clone();
            window.on_window_event(move |event| {
                if matches!(event, tauri::WindowEvent::Destroyed) {
                    close_all_rooms(&app);
                }
            });
            Some(window)
        }
        Err(e) => {
            engine::append_log(
                &engine::app_data_root()
                    .unwrap_or_default()
                    .join("logs")
                    .join("shell.log"),
                &format!("open browser console failed: {e}"),
            );
            None
        }
    }
}

/// 把主窗口的 window.open 目标交给应用内浏览器：先入队，再打开/聚焦窗口。
/// 页面可能还没加载完，所以不在 Rust 侧 eval，由浏览器页轮询取走。
pub fn browser_enqueue(app: &AppHandle, url: &str, capture: bool) {
    browser_enqueue_tab(
        app,
        PendingTab {
            url: url.to_string(),
            capture,
            sid: String::new(),
            room: String::new(),
            action: String::new(),
        },
    );
}

pub fn browser_enqueue_tab(app: &AppHandle, tab: PendingTab) {
    browser_enqueue_tab_ex(app, tab, true);
}

/// `show=false` 用于单窗口壳的后台采集：窗口存在但不显示。
pub fn browser_enqueue_tab_ex(app: &AppHandle, tab: PendingTab, show: bool) {
    let closing = tab.action == "close";
    if let Some(state) = app.try_state::<RoomState>() {
        state.pending.lock().unwrap().push(tab);
    }
    // 关标签页不该把已经关掉的浏览器窗口又拉起来；打开标签页才需要窗口。
    if !closing {
        let _ = open_browser_console_ex(app, show);
    }
}

fn host_of(url: &str) -> String {
    tauri::Url::parse(url)
        .ok()
        .and_then(|u| u.host_str().map(|h| h.to_string()))
        .unwrap_or_else(|| url.chars().take(40).collect())
}

fn watcher_log(msg: &str) {
    if let Ok(root) = engine::app_data_root() {
        engine::append_log(&root.join("logs").join("shell.log"), msg);
    }
}

/// 设置页的采集方式。非 browser 时房间由引擎侧的采集代理接管，壳不要抢。
fn fetch_capture_mode(port: u16) -> String {
    let Ok(resp) = http::get(port, "/api/settings", Duration::from_secs(2)) else {
        return "browser".into();
    };
    let Ok(v) = resp.json() else {
        return "browser".into();
    };
    match v
        .get("prefs")
        .and_then(|p| p.get("capture_mode"))
        .and_then(|m| m.as_str())
    {
        Some("direct") => "direct".into(),
        Some("external") => "external".into(),
        _ => "browser".into(),
    }
}

/// 桌面自己接住引擎房间：飞书机器人或收听页 `POST /api/rooms` 之后，
/// 不再拉外部 Chrome，而是建一个采集会话，把直播间交给应用内浏览器。
///
/// 房间停止（飞书 `/stop`、收听页停止、标签页手动关）时通知浏览器关标签页；
/// 浏览器窗口关掉时 `close_all_rooms` 会反过来把房间落到 stopped。
pub fn start_room_watcher(app: AppHandle, port: u16, background: bool) {
    std::thread::spawn(move || {
        // room id -> desktop / external / closed
        let mut owned: HashMap<String, String> = HashMap::new();
        let mut retry_at: HashMap<String, Instant> = HashMap::new();
        loop {
            std::thread::sleep(Duration::from_secs(2));
            let capture_mode = fetch_capture_mode(port);
            let Ok(resp) = http::get(port, "/api/rooms", Duration::from_secs(3)) else {
                continue;
            };
            let Ok(v) = resp.json() else { continue };
            let Some(rows) = v.get("rooms").and_then(|r| r.as_array()) else {
                continue;
            };
            let mut seen: HashMap<String, String> = HashMap::new();
            for r in rows {
                let id = r
                    .get("id")
                    .and_then(|x| x.as_str())
                    .unwrap_or("")
                    .to_string();
                if id.is_empty() {
                    continue;
                }
                let state = r
                    .get("state")
                    .and_then(|x| x.as_str())
                    .unwrap_or("")
                    .to_string();
                seen.insert(id.clone(), state.clone());
                let ours = owned.get(&id).cloned();
                match state.as_str() {
                    "pending" => {
                        // direct / external 由引擎侧的采集代理接房间；
                        // browser 才由桌面壳开应用内浏览器标签页。
                        if capture_mode != "browser" {
                            continue;
                        }
                        if ours.is_some() {
                            continue;
                        }
                        if retry_at
                            .get(&id)
                            .map(|t| t.elapsed() < Duration::from_secs(10))
                            .unwrap_or(false)
                        {
                            continue;
                        }
                        let url = r
                            .get("url")
                            .and_then(|x| x.as_str())
                            .unwrap_or("")
                            .to_string();
                        if url.is_empty() {
                            continue;
                        }
                        let title = format!("桌面 · {}", host_of(&url));
                        let sid = match http::post_json(
                            port,
                            "/api/live/start",
                            &json!({ "title": title, "room_id": id }),
                            Duration::from_secs(5),
                        ) {
                            Ok(resp) => resp
                                .json()
                                .ok()
                                .and_then(|v| {
                                    v.get("session_id")
                                        .and_then(|s| s.as_str())
                                        .map(|s| s.to_string())
                                })
                                .unwrap_or_default(),
                            Err(e) => {
                                watcher_log(&format!("room watcher: live/start {id} failed: {e}"));
                                String::new()
                            }
                        };
                        if sid.is_empty() {
                            retry_at.insert(id.clone(), Instant::now());
                            continue;
                        }
                        retry_at.remove(&id);
                        let _ = http::post_json(
                            port,
                            "/api/rooms/state",
                            &json!({
                                "id": id,
                                "state": "starting",
                                "stage": 2,
                                "session_id": sid,
                                "engine": "desktop",
                                "stage_note": "应用内浏览器打开中",
                            }),
                            Duration::from_secs(3),
                        );
                        browser_enqueue_tab_ex(
                            &app,
                            PendingTab {
                                url: url.clone(),
                                capture: true,
                                sid: sid.clone(),
                                room: id.clone(),
                                action: String::new(),
                            },
                            !background,
                        );
                        owned.insert(id.clone(), "desktop".into());
                        watcher_log(&format!(
                            "room watcher: {id} -> desktop tab {url} sid={sid}"
                        ));
                    }
                    "starting" | "live" => {
                        // 已经有采集端在跑（例如用户手动开了网页版代理）就别抢。
                        if ours.is_none() {
                            owned.insert(id.clone(), "external".into());
                        }
                    }
                    "stopped" | "failed" => {
                        if ours.as_deref() == Some("desktop") {
                            browser_enqueue_tab(
                                &app,
                                PendingTab {
                                    url: String::new(),
                                    capture: true,
                                    sid: String::new(),
                                    room: id.clone(),
                                    action: "close".into(),
                                },
                            );
                            owned.insert(id.clone(), "closed".into());
                            watcher_log(&format!("room watcher: {id} stopped -> close tab"));
                        }
                    }
                    _ => {}
                }
            }
            // 房间被直接删掉（不是 stopped）也要关标签页。
            for (id, prev) in owned.clone() {
                if prev == "desktop" && !seen.contains_key(&id) {
                    browser_enqueue_tab(
                        &app,
                        PendingTab {
                            url: String::new(),
                            capture: true,
                            sid: String::new(),
                            room: id.clone(),
                            action: "close".into(),
                        },
                    );
                    watcher_log(&format!("room watcher: {id} removed -> close tab"));
                    owned.insert(id, "closed".into());
                }
            }
            if owned.len() > 200 {
                owned.retain(|_, v| v != "closed");
                retry_at.clear();
            }
        }
    });
}

/// 采集标签页报告房间状态：第一块音频到了才报 live，飞书的「开好了我喊你」
/// 不会提前喊。
#[tauri::command]
pub fn room_report(
    state: State<'_, RoomState>,
    room: String,
    sid: String,
    status: String,
) -> Result<(), String> {
    if room.is_empty() {
        return Ok(());
    }
    let body = json!({
        "id": room,
        "session_id": sid,
        "state": status,
        "engine": "desktop",
    });
    http::post_json(state.port, "/api/rooms/state", &body, Duration::from_secs(3))
        .map(|_| ())
        .map_err(|e| e.to_string())
}

/// 用户手动关采集标签页时，把房间也停掉，否则飞书里还显示「监听中」。
#[tauri::command]
pub fn room_close_room(state: State<'_, RoomState>, room: String) -> Result<(), String> {
    if room.is_empty() {
        return Ok(());
    }
    http::post_json(
        state.port,
        "/api/rooms/close",
        &json!({ "id": room }),
        Duration::from_secs(5),
    )
    .map(|_| ())
    .map_err(|e| e.to_string())
}

/// 浏览器窗口关闭时把子视图和引擎会话一起收口：否则页面没了，
/// 引擎还会挂着一个 running 会话等看门狗。
pub fn close_all_rooms(app: &AppHandle) {
    let Some(state) = app.try_state::<RoomState>() else {
        return;
    };
    let port = state.port;
    let rooms: Vec<(String, Webview)> = state.rooms.lock().unwrap().drain().collect();
    let sids: Vec<String> = state
        .sessions
        .lock()
        .unwrap()
        .drain()
        .map(|(_, sid)| sid)
        .collect();
    // 桌面接住的房间：关浏览器窗口等于停止监听，房间也要落到 stopped，
    // 否则飞书里 /rooms 会一直显示「监听中」。
    let room_ids: Vec<String> = state
        .tab_rooms
        .lock()
        .unwrap()
        .drain()
        .map(|(_, rid)| rid)
        .collect();
    state.stats.lock().unwrap().clear();
    for (label, webview) in rooms {
        state.closing.lock().unwrap().insert(label, Instant::now());
        let _ = webview.close();
    }
    if sids.iter().any(|s| !s.is_empty()) || !room_ids.is_empty() {
        std::thread::spawn(move || {
            for sid in sids {
                if sid.is_empty() {
                    continue;
                }
                let body = json!({ "session_id": sid });
                let _ = http::post_json(port, "/api/live/stop", &body, Duration::from_secs(10));
            }
            for rid in room_ids {
                let _ = http::post_json(
                    port,
                    "/api/rooms/close",
                    &json!({ "id": rid }),
                    Duration::from_secs(10),
                );
            }
        });
    }
}

fn attach_message_handler(
    webview: &Webview,
    room: &str,
    tx: Sender<ChunkJob>,
) -> Result<(), String> {
    let room = room.to_string();
    webview
        .with_webview(move |pw| {
            #[cfg(windows)]
            unsafe {
                use webview2_com::{take_pwstr, WebMessageReceivedEventHandler};
                use windows::core::PWSTR;

                let Ok(core) = pw.controller().CoreWebView2() else {
                    return;
                };
                let tx = tx.clone();
                let room_for_handler = room.clone();
                let handler =
                    WebMessageReceivedEventHandler::create(Box::new(move |_sender, args| {
                        let Some(args) = args else {
                            return Ok(());
                        };
                        let mut ptr = PWSTR::null();
                        if args.TryGetWebMessageAsString(&mut ptr).is_ok() {
                            let msg = take_pwstr(ptr);
                            if !msg.is_empty() {
                                let _ = tx.send(ChunkJob {
                                    room: room_for_handler.clone(),
                                    raw: msg,
                                });
                            }
                        }
                        Ok(())
                    }));
                let mut token = 0i64;
                let result = core.add_WebMessageReceived(&handler, &mut token);
                if let Some(path) = engine::app_data_root()
                    .ok()
                    .map(|p| p.join("logs").join("shell.log"))
                {
                    engine::append_log(
                        &path,
                        &format!("attach_message_handler {room} result={result:?} token={token}"),
                    );
                }
            }
            #[cfg(not(windows))]
            {
                let _ = (pw, room, tx);
            }
        })
        .map_err(|e| e.to_string())
}

/// 监听 WebView2 的 ProcessFailed：渲染进程崩了、卡死、浏览器进程退出都会到这里。
///
/// 渲染进程崩溃时 WebView2 官方建议直接 Reload；这里做有限次自动重载
/// （同一房间 10 分钟最多 3 次），超过就只提示“需要重新打开这一路”，
/// 避免页面脚本把壳拖进无限崩溃—重载循环。
fn attach_crash_handler(
    webview: &Webview,
    room: &str,
    app: AppHandle,
    closing: Arc<Mutex<HashMap<String, Instant>>>,
) -> Result<(), String> {
    let room = room.to_string();
    webview
        .with_webview(move |pw| {
            #[cfg(windows)]
            unsafe {
                use webview2_com::Microsoft::Web::WebView2::Win32::{
                    ICoreWebView2ProcessFailedEventArgs2, COREWEBVIEW2_PROCESS_FAILED_KIND,
                    COREWEBVIEW2_PROCESS_FAILED_REASON,
                };
                use webview2_com::{take_pwstr, ProcessFailedEventHandler};
                use windows::core::{Interface as _, PWSTR};

                let Ok(core) = pw.controller().CoreWebView2() else {
                    return;
                };
                let app = app.clone();
                let room = room.clone();
                let room_for_log = room.clone();
                let closing = closing.clone();
                let core_for_reload = core.clone();
                let handler =
                    ProcessFailedEventHandler::create(Box::new(move |_sender, args| {
                        let Some(args) = args else {
                            return Ok(());
                        };
                        // 主动关闭房间时 WebView2 也会走销毁流程，不能当成崩溃。
                        let expected = closing
                            .lock()
                            .map(|m| {
                                m.get(&room)
                                    .map(|t| t.elapsed() < Duration::from_secs(10))
                                    .unwrap_or(false)
                            })
                            .unwrap_or(false);
                        if expected {
                            return Ok(());
                        }

                        let mut kind = COREWEBVIEW2_PROCESS_FAILED_KIND(9);
                        let _ = args.ProcessFailedKind(&mut kind);
                        let mut reason = COREWEBVIEW2_PROCESS_FAILED_REASON(0);
                        let mut exit_code = 0i32;
                        let mut desc = String::new();
                        if let Ok(args2) =
                            args.cast::<ICoreWebView2ProcessFailedEventArgs2>()
                        {
                            let _ = args2.Reason(&mut reason);
                            let _ = args2.ExitCode(&mut exit_code);
                            let mut ptr = PWSTR::null();
                            if args2.ProcessDescription(&mut ptr).is_ok() && !ptr.is_null() {
                                desc = take_pwstr(ptr);
                            }
                        }
                        let kind_name = match kind.0 {
                            0 => "browser_process_exited",
                            1 => "render_process_exited",
                            2 => "render_process_unresponsive",
                            3 => "frame_render_process_exited",
                            4 => "utility_process_exited",
                            5 => "sandbox_helper_process_exited",
                            6 => "gpu_process_exited",
                            7 => "ppapi_plugin_process_exited",
                            8 => "ppapi_broker_process_exited",
                            _ => "unknown_process_exited",
                        };
                        let reason_name = match reason.0 {
                            0 => "unexpected",
                            1 => "unresponsive",
                            2 => "terminated",
                            3 => "crashed",
                            4 => "launch_failed",
                            5 => "out_of_memory",
                            6 => "profile_deleted",
                            _ => "unknown",
                        };
                        // 浏览器进程退出后 WebView 已经不可用，Reload 没有意义；
                        // 渲染进程 / unresponsive 可以尝试恢复。
                        let can_reload = matches!(kind.0, 1 | 2 | 3);
                        let detail = format!(
                            "kind={kind_name} reason={reason_name}({}) exit_code={exit_code} desc={desc}",
                            reason.0
                        );
                        let reload =
                            diag::room_failed(&app, &room, kind_name, &detail, can_reload);
                        if reload {
                            if let Err(e) = core_for_reload.Reload() {
                                if let Ok(root) = engine::app_data_root() {
                                    engine::append_log(
                                        &root.join("logs").join("shell.log"),
                                        &format!("room {room} reload failed: {e}"),
                                    );
                                }
                            } else if let Ok(root) = engine::app_data_root() {
                                engine::append_log(
                                    &root.join("logs").join("shell.log"),
                                    &format!("room {room} reloaded after process failure"),
                                );
                            }
                        }
                        Ok(())
                    }));
                let mut token = 0i64;
                let result = core.add_ProcessFailed(&handler, &mut token);
                if let Ok(root) = engine::app_data_root() {
                    engine::append_log(
                        &root.join("logs").join("shell.log"),
                        &format!("attach_crash_handler {room_for_log} result={result:?} token={token}"),
                    );
                }
            }
            #[cfg(not(windows))]
            {
                let _ = (pw, room, app, closing);
            }
        })
        .map_err(|e| e.to_string())
}

/// 单房间发送速率上限：4 块/秒是正常值，给到 25 块/秒仍能容纳网络抖动；
/// 超过就丢块并计入 drops，避免被页面脚本拖垮引擎。
const MAX_CHUNKS_PER_SEC: u32 = 25;
const MAX_CHUNK_BYTES: usize = 48 * 1024;

pub fn spawn_chunk_worker(
    port: u16,
    rx: Receiver<ChunkJob>,
    sessions: Arc<Mutex<HashMap<String, String>>>,
    stats: Arc<Mutex<HashMap<String, RoomStat>>>,
) {
    std::thread::Builder::new()
        .name("live-chunk-forwarder".into())
        .spawn(move || {
            let mut windows: HashMap<String, (Instant, u32)> = HashMap::new();
            while let Ok(job) = rx.recv() {
                let value: serde_json::Value = match serde_json::from_str(&job.raw) {
                    Ok(v) => v,
                    Err(_) => continue,
                };
                let kind = value.get("type").and_then(|v| v.as_str()).unwrap_or("");
                if kind == "capture_state" {
                    let state = value.get("state").and_then(|v| v.as_str()).unwrap_or("");
                    let title = value.get("title").and_then(|v| v.as_str()).unwrap_or("");
                    let cookie = value.get("cookie").and_then(|v| v.as_str()).unwrap_or("");
                    let shell_log = engine::app_data_root()
                        .ok()
                        .map(|p| p.join("logs").join("shell.log"));
                    if let Some(path) = shell_log {
                        engine::append_log(
                            &path,
                            &format!(
                                "room {} state={} title={:?} cookie={:?}",
                                job.room,
                                state,
                                title.chars().take(80).collect::<String>(),
                                cookie.chars().take(160).collect::<String>()
                            ),
                        );
                    }
                    continue;
                }
                if kind != "live_chunk" {
                    continue;
                }
                let msg_room = value.get("room").and_then(|v| v.as_str()).unwrap_or("");
                if msg_room != job.room {
                    continue;
                }
                let encoded = value.get("pcm").and_then(|v| v.as_str()).unwrap_or("");
                if encoded.is_empty() || encoded.len() > MAX_CHUNK_BYTES * 2 {
                    bump_drop(&stats, &job.room);
                    continue;
                }
                let pcm = match base64::engine::general_purpose::STANDARD.decode(encoded) {
                    Ok(v) => v,
                    Err(_) => {
                        bump_drop(&stats, &job.room);
                        continue;
                    }
                };
                if pcm.is_empty() || pcm.len() > MAX_CHUNK_BYTES || pcm.len() % 2 != 0 {
                    bump_drop(&stats, &job.room);
                    continue;
                }

                let now = Instant::now();
                let entry = windows.entry(job.room.clone()).or_insert((now, 0));
                if now.duration_since(entry.0) >= Duration::from_secs(1) {
                    *entry = (now, 0);
                }
                entry.1 += 1;
                if entry.1 > MAX_CHUNKS_PER_SEC {
                    bump_drop(&stats, &job.room);
                    continue;
                }

                let sid = sessions.lock().unwrap().get(&job.room).cloned();
                let Some(sid) = sid.filter(|s| !s.is_empty()) else {
                    bump_drop(&stats, &job.room);
                    continue;
                };

                match http::post_bytes(
                    port,
                    "/api/live/chunk",
                    "application/octet-stream",
                    &[("X-Live-Id", sid.clone())],
                    &pcm,
                    Duration::from_secs(5),
                ) {
                    Ok(resp) if resp.status == 200 => {
                        if let Some(stat) = stats.lock().unwrap().get_mut(&job.room) {
                            stat.chunks += 1;
                            stat.bytes += pcm.len() as u64;
                            stat.last = Some(now);
                        }
                    }
                    _ => bump_drop(&stats, &job.room),
                }
            }
        })
        .expect("spawn live chunk forwarder");
}

fn bump_drop(stats: &Arc<Mutex<HashMap<String, RoomStat>>>, room: &str) {
    if let Some(stat) = stats.lock().unwrap().get_mut(room) {
        stat.drops += 1;
    }
}

fn capture_script(room: &str) -> String {
    CAPTURE_JS.replace("__LN_ROOM__", room)
}

/// 注入脚本：找页面里正在播放的 audio/video，captureStream -> 16k 单声道 PCM，
/// 攒 4096 样本（256ms）走 WebView2 web message 回传。
///
/// 不用 fetch 本地端口：直播页的 CSP 可能不允许 connect-src 127.0.0.1，而且
/// 不该把引擎端口暴露给页面脚本。`window.chrome.webview` 是宿主 API，不受 CSP 管。
const CAPTURE_JS: &str = r#"
(function () {
  if (window.__LN_CAPTURE__) return;
  window.__LN_CAPTURE__ = true;
  var ROOM = "__LN_ROOM__";
  var TARGET = 16000;
  var sent = 0, bytes = 0, errors = 0;
  var ctx = null, srcNode = null, proc = null, gainNode = null, media = null, stream = null;
  var badgeEl = null;
  var timer = null;

  function badge(text, good) {
    try {
      if (!document.documentElement) return;
      if (!badgeEl) {
        badgeEl = document.createElement('div');
        badgeEl.style.cssText = 'position:fixed;right:10px;top:10px;z-index:2147483647;' +
          'background:rgba(18,24,38,.9);color:#fff;font:12px/1.5 "Microsoft YaHei",system-ui;' +
          'padding:6px 10px;border-radius:8px;pointer-events:none;max-width:360px;white-space:pre-wrap;' +
          'box-shadow:0 4px 18px rgba(0,0,0,.18)';
        document.documentElement.appendChild(badgeEl);
      }
      badgeEl.textContent = 'LiveNotes · ' + text;
      badgeEl.style.background = good ? 'rgba(10,122,74,.93)' : 'rgba(179,38,30,.93)';
    } catch (e) {}
  }

  function post(obj) {
    try {
      if (window.chrome && window.chrome.webview && window.chrome.webview.postMessage) {
        window.chrome.webview.postMessage(JSON.stringify(obj));
        return true;
      }
    } catch (e) {}
    return false;
  }

  function report(state) {
    post({
      type: 'capture_state', room: ROOM, state: state,
      title: document.title || '', cookie: document.cookie || '',
      chunks: sent, bytes: bytes, errors: errors
    });
  }

  function toBase64(buf) {
    var bytes = new Uint8Array(buf), out = '', step = 0x8000;
    for (var i = 0; i < bytes.length; i += step) {
      out += String.fromCharCode.apply(null, bytes.subarray(i, Math.min(i + step, bytes.length)));
    }
    return btoa(out);
  }

  function pickMedia() {
    var all = Array.prototype.slice.call(document.querySelectorAll('video,audio'));
    all = all.filter(function (m) {
      try { return m.readyState >= 2 && !m.paused && !m.ended; } catch (e) { return false; }
    });
    all.sort(function (a, b) {
      return (b.videoWidth || 0) * (b.videoHeight || 0) - (a.videoWidth || 0) * (a.videoHeight || 0);
    });
    return all[0] || null;
  }

  function reset() {
    try { if (proc) proc.disconnect(); } catch (e) {}
    try { if (srcNode) srcNode.disconnect(); } catch (e) {}
    try { if (gainNode) gainNode.disconnect(); } catch (e) {}
    try { if (stream) stream.getTracks().forEach(function (t) { t.stop(); }); } catch (e) {}
    try { if (ctx) ctx.close(); } catch (e) {}
    proc = srcNode = gainNode = media = stream = ctx = null;
  }

  async function start() {
    if (ctx) return;
    media = pickMedia();
    if (!media) {
      badge('等待页面播放音频…', false);
      setTimeout(start, 1500);
      return;
    }
    if (!media.captureStream && !media.mozCaptureStream) {
      badge('当前内核不支持 captureStream', false);
      return;
    }
    try { await media.play(); } catch (e) {}
    try {
      stream = (media.captureStream || media.mozCaptureStream).call(media);
    } catch (e) {
      badge('captureStream 失败：' + e.message, false);
      setTimeout(start, 2000);
      return;
    }
    var tracks = stream.getAudioTracks ? stream.getAudioTracks() : [];
    if (!tracks.length) {
      badge('媒体没有音轨，等待中…', false);
      reset();
      setTimeout(start, 1500);
      return;
    }
    try { ctx = new AudioContext({ sampleRate: TARGET }); } catch (e) { ctx = new AudioContext(); }
    if (ctx.state === 'suspended') { try { await ctx.resume(); } catch (e) {} }
    var inRate = ctx.sampleRate || 48000;
    var phase = 0, sum = 0, cnt = 0;
    var pcm = new Int16Array(4096), n = 0;

    srcNode = ctx.createMediaStreamSource(stream);
    proc = ctx.createScriptProcessor(4096, 1, 1);
    proc.onaudioprocess = function (ev) {
      var input = ev.inputBuffer.getChannelData(0);
      for (var i = 0; i < input.length; i++) {
        sum += input[i]; cnt++; phase += TARGET / inRate;
        if (phase >= 1) {
          var v = sum / cnt; sum = 0; cnt = 0; phase -= 1;
          v = v < -1 ? -1 : (v > 1 ? 1 : v);
          pcm[n++] = v < 0 ? v * 0x8000 : v * 0x7fff;
          if (n >= pcm.length) {
            var copy = pcm.slice(0, n);
            sent++; bytes += copy.byteLength;
            var ok = post({ type: 'live_chunk', room: ROOM, pcm: toBase64(copy.buffer) });
            if (!ok) errors++;
            n = 0;
          }
        }
      }
    };
    gainNode = ctx.createGain();
    gainNode.gain.value = 0;
    srcNode.connect(proc);
    proc.connect(gainNode);
    gainNode.connect(ctx.destination);
    tracks[0].onended = function () { reset(); setTimeout(start, 800); };
    media.addEventListener('ended', function () { reset(); setTimeout(start, 800); });
    badge('采集中 · ' + inRate + 'Hz → 16kHz', true);
    report('capturing');
  }

  setTimeout(function () { report('page_ready'); }, 400);
  setInterval(function () { report('heartbeat'); }, 10000);
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start);
  } else {
    start();
  }
  // 页面可能是 SPA：切换集数后 <video> 会换元素，定期检查媒体是否还在。
  setInterval(function () {
    if (ctx && media && (!media.isConnected || media.ended)) { reset(); start(); }
    if (ctx) badge('采集中 · 已发 ' + sent + ' 块 · ' + (bytes / 32000).toFixed(1) + 's', true);
  }, 5000);
  document.addEventListener('pointerdown', function once() {
    document.removeEventListener('pointerdown', once);
    if (ctx && ctx.state === 'suspended') ctx.resume();
    if (!ctx) start();
  }, { once: true });
})();
"#;
