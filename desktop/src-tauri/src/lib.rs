mod diag;
mod engine;
mod http;
mod rooms;

use std::collections::HashMap;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::mpsc;
use std::sync::{Arc, Mutex};

use tauri::{
    menu::{Menu, MenuItem, PredefinedMenuItem},
    tray::{TrayIconBuilder, TrayIconEvent},
    webview::NewWindowResponse,
    Emitter, Manager, RunEvent, WebviewUrl, WebviewWindowBuilder,
};
use tauri_plugin_opener::OpenerExt;

pub struct ShellState {
    pub engine: engine::Engine,
}

/// 关闭请求序号 / 壳页处理回执。壳页处理关闭时回执；2 秒没回执就退回
/// 「最小化到托盘」，避免错误页或壳页卡住时关不掉窗口。
static CLOSE_SEQ: AtomicU64 = AtomicU64::new(0);
static CLOSE_ACK: AtomicU64 = AtomicU64::new(0);

fn ack_close() {
    CLOSE_ACK.store(CLOSE_SEQ.load(Ordering::SeqCst), Ordering::SeqCst);
}

/// 启动早期也落盘：窗口/WebView2 创建失败时 stderr 可能随 GUI 启动方式丢掉，
/// 这个日志是 D1 的取证入口之一。
#[cfg(windows)]
fn apply_dark_titlebar(window: &tauri::WebviewWindow) {
    use windows::Win32::Graphics::Dwm::{DwmSetWindowAttribute, DWMWA_USE_IMMERSIVE_DARK_MODE};
    if let Ok(hwnd) = window.hwnd() {
        let on: i32 = 1;
        unsafe {
            let _ = DwmSetWindowAttribute(
                hwnd,
                DWMWA_USE_IMMERSIVE_DARK_MODE,
                &on as *const _ as *const std::ffi::c_void,
                std::mem::size_of::<i32>() as u32,
            );
        }
    }
}

/// 接管 WebView2 的浏览器行为：关掉原生右键菜单，正式版再关掉
/// F5/Ctrl+R/Ctrl+P 这类浏览器快捷键和 devtools；debug 构建保留，方便排查。
/// 只用于应用自己的窗口（main / rooms / live-* / browser 控制台），
/// 不用在抖音采集页上——那边保留浏览器行为，避免影响登录和复制。
#[cfg(windows)]
pub(crate) fn tame_webview(window: &tauri::WebviewWindow) {
    let debug = cfg!(debug_assertions);
    let _ = window.with_webview(move |pw| {
        #[cfg(windows)]
        unsafe {
            use webview2_com::Microsoft::Web::WebView2::Win32::ICoreWebView2Settings3;
            use windows::core::Interface as _;

            if let Ok(core) = pw.controller().CoreWebView2() {
                if let Ok(settings) = core.Settings() {
                    let _ = settings.SetAreDefaultContextMenusEnabled(false);
                    let _ = settings.SetAreDevToolsEnabled(debug);
                    // AreBrowserAcceleratorKeysEnabled 在 Settings3 上；老运行时没有就跳过。
                    if let Ok(s3) = settings.cast::<ICoreWebView2Settings3>() {
                        let _ = s3.SetAreBrowserAcceleratorKeysEnabled(debug);
                    }
                }
            }
        }
    });
}

#[cfg(not(windows))]
pub(crate) fn tame_webview(_window: &tauri::WebviewWindow) {}

fn boot_log(msg: &str) {
    use std::io::Write;
    use std::time::{SystemTime, UNIX_EPOCH};

    let ts = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    let mut paths = Vec::new();
    if let Some(home) = std::env::var_os("LIVENOTES_HOME") {
        paths.push(std::path::PathBuf::from(home).join("logs/shell.log"));
    } else if let Ok(base) = std::env::var("LOCALAPPDATA") {
        paths.push(std::path::PathBuf::from(base).join("LiveNotes/logs/shell.log"));
    }
    paths.push(std::env::temp_dir().join("livenotes-boot.log"));
    paths.push(std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("boot.log"));

    let mut errors = String::new();
    for path in paths {
        if let Some(parent) = path.parent() {
            if let Err(e) = std::fs::create_dir_all(parent) {
                errors.push_str(&format!("\n  mkdir {parent:?}: {e}"));
                continue;
            }
        }
        match std::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(&path)
        {
            Ok(mut f) => {
                let _ = writeln!(f, "[{ts}] {msg}");
            }
            Err(e) => errors.push_str(&format!("\n  open {path:?}: {e}")),
        }
    }
    if !errors.is_empty() {
        let local = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("boot.log");
        if let Ok(mut f) = std::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(local)
        {
            let _ = writeln!(f, "[{ts}] boot_log failures:{errors}");
        }
    }
}

/// 建启动页。单独抽出来是为了在 WebView2 profile 被残留句柄锁住时，
/// 能换一个全新目录重试一次（见 setup 里的 splash 分支）。
fn build_splash(
    app: &tauri::App,
    label: &str,
    profile: &str,
) -> Result<tauri::WebviewWindow, String> {
    WebviewWindowBuilder::new(app, label, WebviewUrl::App("index.html".into()))
        .data_directory(engine::webview_dir(profile)?)
        .title("LiveNotes / 直播语记")
        .inner_size(520.0, 300.0)
        .center()
        .resizable(false)
        // 启动画面也去掉系统标题条，避免深色界面前闪一下蓝色。
        .decorations(false)
        .build()
        .map_err(|e| e.to_string())
}

/// 读桌面新窗口偏好：`external` / `internal`；引擎不可用或没设置时返回 None。
fn fetch_open_mode(port: u16) -> Option<String> {
    let resp = http::get(port, "/api/settings", std::time::Duration::from_secs(2)).ok()?;
    let v = resp.json().ok()?;
    let raw = v
        .get("prefs")
        .and_then(|p| p.get("open_mode"))
        .and_then(|m| m.as_str())?;
    Some(if raw == "external" { "external" } else { "internal" }.to_string())
}

pub fn run() {
    boot_log(&format!(
        "run start exe={:?} cwd={:?} localappdata={:?}",
        std::env::current_exe(),
        std::env::current_dir(),
        std::env::var("LOCALAPPDATA")
    ));
    let builder = tauri::Builder::default();
    // 自动化/并行调试时允许跳过单实例锁；release 始终单实例。
    let builder = if cfg!(debug_assertions)
        && std::env::var("LIVENOTES_ALLOW_MULTI").as_deref() == Ok("1")
    {
        builder
    } else {
        builder.plugin(tauri_plugin_single_instance::init(|app, _argv, _cwd| {
            show_main(app);
        }))
    };
    let builder = builder
        .plugin(tauri_plugin_opener::init())
        .invoke_handler(tauri::generate_handler![
            rooms::room_open,
            rooms::room_close,
            rooms::room_layout,
            rooms::room_show,
            rooms::browser_take_pending,
            rooms::room_navigate,
            rooms::room_reload,
            rooms::room_history,
            rooms::room_url,
            rooms::room_focus,
            rooms::room_mute,
            rooms::room_stats,
            rooms::room_report,
            rooms::room_close_room,
            rooms::live_start,
            rooms::live_stop,
            rooms::live_open_view,
            rooms::live_probe,
            spike_report,
            shell_close_ack,
            shell_hide,
            shell_quit,
            diag::diag_snapshot,
            diag::diag_export,
            diag::diag_open_logs,
            diag::diag_open_window,
            diag::diag_engine_kill,
        ])
        .setup(|app| {
            // 诊断状态必须最早注册：引擎启动失败时错误页要能调 diag_export，
            // 托盘也可能在任何窗口创建之前就需要显示「正在启动」。
            app.manage(diag::Diag::new());
            boot_log("setup: creating splash window");
            eprintln!("[setup] creating splash window");
            let window = match build_splash(app, "splash", "splash") {
                Ok(w) => w,
                Err(e) => {
                    boot_log(&format!("setup: splash build failed: {e}"));
                    eprintln!("[setup] splash build failed: {e}");
                    let in_use = e.contains("0x800700AA")
                        || e.contains("在使用中")
                        || e.contains("in use");
                    if !in_use {
                        return Err(e.into());
                    }
                    // 上次崩溃/残留句柄把 splash profile 锁死时，换一个按进程号
                    // 命名的全新目录重试。启动页没有登录态，换目录无副作用。
                    let retry_profile = format!("splash-{}", std::process::id());
                    boot_log(&format!(
                        "setup: retry splash with profile {retry_profile}"
                    ));
                    build_splash(app, "splash-retry", &retry_profile).map_err(|e2| {
                        boot_log(&format!("setup: splash retry failed: {e2}"));
                        eprintln!("[setup] splash retry failed: {e2}");
                        e2
                    })?
                }
            };
            boot_log("setup: splash ok");
            eprintln!("[setup] splash ok");
            let _ = window.set_focus();

            // 托盘要在引擎启动之前就存在：引擎起不来时，用户唯一的入口就是托盘
            // 和错误页，不能等引擎健康检查通过之后才创建。
            eprintln!("[setup] creating tray");
            setup_tray(app).map_err(|e| {
                eprintln!("[setup] tray failed: {e}");
                e
            })?;
            diag::engine_starting(app.handle());

            let handle = app.handle().clone();
            let shell_log = engine::app_data_root()
                .map(|p| p.join("logs").join("shell.log"))
                .ok();
            if let Some(path) = &shell_log {
                engine::append_log(path, "shell setup: starting engine");
            }

            eprintln!("[setup] starting engine");
            let engine = match engine::Engine::acquire(&handle) {
                Ok(e) => e,
                Err(err) => {
                    eprintln!("[setup] engine start failed: {err}");
                    if let Some(path) = &shell_log {
                        engine::append_log(path, &format!("engine start failed: {err}"));
                    }
                    diag::engine_start_failed(app.handle(), &err.to_string());
                    let _ = window.close();
                    let mut error_window = WebviewWindowBuilder::new(
                        app,
                        "main",
                        WebviewUrl::App("error.html".into()),
                    )
                    .title("LiveNotes · 引擎未启动")
                    .inner_size(760.0, 460.0)
                    .center();
                    if let Ok(dir) = engine::webview_dir("error") {
                        error_window = error_window.data_directory(dir);
                    }
                    let _ = error_window.build();
                    return Ok(());
                }
            };

            let url = engine.base_url();
            if let Some(path) = &shell_log {
                engine::append_log(
                    path,
                    &format!("engine ready on 127.0.0.1:{}", engine.port),
                );
                engine::append_log(path, &format!("engine ready, loading {url}"));
            }
            let port = engine.port;
            let (chunk_tx, chunk_rx) = mpsc::channel();
            let sessions = Arc::new(Mutex::new(HashMap::new()));
            let stats = Arc::new(Mutex::new(HashMap::new()));
            rooms::spawn_chunk_worker(port, chunk_rx, sessions.clone(), stats.clone());
            app.manage(rooms::RoomState {
                port,
                tx: chunk_tx,
                rooms: Mutex::new(HashMap::new()),
                sessions,
                stats,
                closing: Arc::new(Mutex::new(HashMap::new())),
                pending: Arc::new(Mutex::new(Vec::new())),
                tab_rooms: Arc::new(Mutex::new(HashMap::new())),
            });
            // 飞书机器人/收听页建的房间由桌面自己接住：开应用内浏览器标签页，
            // 不再拉外部 Chrome（网页版才用 capture_agent）。单窗口壳里采集
            // 浏览器只做后台宿主，不弹窗抢焦点。
            let shell_ui = std::env::var("LIVENOTES_LEGACY_UI").as_deref() != Ok("1")
                && matches!(
                    std::env::var("LIVENOTES_SPIKE").ok().as_deref(),
                    None | Some("m1shell")
                );
            rooms::start_room_watcher(app.handle().clone(), port, shell_ui);
            diag::engine_ready(app.handle(), port, engine.pid, engine.started_at);
            diag::start_watchers(
                app.handle().clone(),
                port,
                engine.child_handle(),
                engine.log_path().to_path_buf(),
            );
            app.manage(ShellState { engine });

            // 桌面默认用应用内浏览器；设置页选 external 时改走系统浏览器。
            // 先同步读一次，避免启动后头两秒内的点击用错默认值；之后每秒刷新。
            let open_mode = Arc::new(Mutex::new(
                fetch_open_mode(port).unwrap_or_else(|| String::from("internal")),
            ));
            let mode_cache = open_mode.clone();
            std::thread::spawn(move || loop {
                std::thread::sleep(std::time::Duration::from_secs(1));
                let Some(mode) = fetch_open_mode(port) else {
                    continue;
                };
                if let Ok(mut cur) = mode_cache.lock() {
                    if cur.as_str() != mode {
                        *cur = mode.clone();
                        boot_log(&format!("open_mode -> {mode}"));
                    }
                }
            });

            let parsed: tauri::Url = url.parse()?;
            eprintln!("[setup] creating main window at {url}");
            // 主窗口默认加载本地壳（tauri.localhost 不受 remote ACL 限制）。
            // 设置 LIVENOTES_LEGACY_UI=1 可回退到旧引擎页；spike 自动化模式
            // 仍按原来的页面跑，避免影响 D2/D3/D4 的按钮路径。
            let spike_mode = std::env::var("LIVENOTES_SPIKE").ok();
            let legacy_ui = std::env::var("LIVENOTES_LEGACY_UI").as_deref() == Ok("1");
            let use_shell = !legacy_ui && matches!(spike_mode.as_deref(), None | Some("m1shell"));
            let engine_init = format!(
                "window.__LN_DESKTOP__ = true; window.__LN_ENGINE__ = {url:?}; window.__LN_ENTRY__ = {url:?};"
            );
            let (main_url, init_script) = if spike_mode.as_deref() == Some("m1local") {
                (
                    WebviewUrl::App("spike-shell.html".into()),
                    format!("window.__LN_DESKTOP__ = true; window.__LN_ENGINE__ = {url:?};"),
                )
            } else if use_shell {
                (WebviewUrl::App("shell.html".into()), engine_init)
            } else {
                (
                    WebviewUrl::External(parsed),
                    "window.__LN_DESKTOP__ = true;".to_string(),
                )
            };
            let shell_mode = use_shell;
            // 网页里的辅助页（文件中心/人物/阅读/高光榜/收听）都用
            // window.open(..., '_blank')。以前一律交给系统浏览器；现在按设置页的
            // 「桌面版 → 新窗口打开方式」走应用内浏览器或系统浏览器。
            let browser_app = app.handle().clone();
            let mode_for_window = open_mode.clone();
            let win = WebviewWindowBuilder::new(app, "main", main_url)
                .data_directory(engine::webview_dir("main")?)
                .title("LiveNotes / 直播语记")
                .inner_size(1440.0, 900.0)
                .min_inner_size(1024.0, 680.0)
                .center()
                // 无边框窗口：标题栏由壳页自绘（见 shell.html 的 .topbar /
                // .win-controls）。这样 Windows 的蓝色系统标题条不会再压在
                // 深色/纸面主题上面，主题切换时整窗是一致的。
                .decorations(false)
                // 给网页一个稳定的桌面标记，设置页据此显示桌面专用选项。
                .initialization_script(&init_script)
                .on_new_window(move |url, _features| {
                    if matches!(url.scheme(), "http" | "https") {
                        // 单窗口壳：引擎自己的链接（收听/文件/人物/实时字幕…）
                        // 交给壳在内容区打开，不再拉第二个窗口。
                        let same_engine = matches!(url.host_str(), Some("127.0.0.1") | Some("localhost"))
                            && url.port() == Some(port);
                        if shell_mode && same_engine {
                            boot_log(&format!("new window -> shell: {url}"));
                            if let Err(e) = browser_app.emit_to("main", "shell_navigate", url.as_str()) {
                                boot_log(&format!("shell navigate emit failed: {e}"));
                                rooms::browser_enqueue(&browser_app, url.as_str(), false);
                            }
                            return NewWindowResponse::Deny;
                        }
                        let mode = mode_for_window
                            .lock()
                            .map(|m| m.clone())
                            .unwrap_or_else(|_| "internal".into());
                        if mode == "external" {
                            boot_log(&format!("new window -> system browser: {url}"));
                            if let Err(e) = browser_app
                                .opener()
                                .open_url(url.as_str(), None::<&str>)
                            {
                                boot_log(&format!("open new window in browser failed: {e}"));
                            }
                        } else {
                            boot_log(&format!("new window -> embedded browser: {url}"));
                            // 主窗口的 window.open 都是应用内辅助页；作为工具页
                            // 打开，不创建采集会话。直播采集走浏览器地址栏。
                            rooms::browser_enqueue(&browser_app, url.as_str(), false);
                        }
                    }
                    NewWindowResponse::Deny
                })
                .build()
                .map_err(|e| {
                    eprintln!("[setup] main window build failed: {e}");
                    e
                })?;
            eprintln!("[setup] main window ok");
            #[cfg(windows)]
            apply_dark_titlebar(&win);
            #[cfg(windows)]
            tame_webview(&win);
            let _ = win.set_focus();
            let _ = window.close();

            // 关主窗口 = 收到托盘，不停止采集。真正退出走托盘「退出 LiveNotes」；
            // 自动化/调试可用 LIVENOTES_CLOSE_TO_TRAY=0 关掉这个拦截。
            // 有录音时壳页会先弹「最小化 / 停止并退出 / 取消」，所以这里不直接
            // hide，只发事件并等回执；2 秒没回执（错误页/壳页卡住）再兜底隐藏。
            let close_app = app.handle().clone();
            win.on_window_event(move |event| {
                if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                    if std::env::var("LIVENOTES_CLOSE_TO_TRAY").as_deref() == Ok("0") {
                        return;
                    }
                    api.prevent_close();
                    let seq = CLOSE_SEQ.fetch_add(1, Ordering::SeqCst) + 1;
                    let emitted = close_app
                        .emit_to("main", "shell_close_requested", seq)
                        .is_ok();
                    if !emitted {
                        if let Some(w) = close_app.get_webview_window("main") {
                            let _ = w.hide();
                        }
                        ack_close();
                        boot_log("main window hidden to tray (no shell)");
                        return;
                    }
                    boot_log("main close requested -> shell");
                    let fallback = close_app.clone();
                    std::thread::spawn(move || {
                        std::thread::sleep(std::time::Duration::from_secs(2));
                        if CLOSE_ACK.load(Ordering::SeqCst) < seq {
                            if let Some(w) = fallback.get_webview_window("main") {
                                let _ = w.hide();
                            }
                            boot_log("main window hidden to tray (no ack)");
                        }
                    });
                }
            });

            // spike 自动化：定时走正常 AppHandle::exit 路径，验证 Exit 回收引擎。
            if let Ok(ms) = std::env::var("LIVENOTES_AUTO_EXIT_MS") {
                if let Ok(ms) = ms.parse::<u64>() {
                    let handle = app.handle().clone();
                    std::thread::spawn(move || {
                        std::thread::sleep(std::time::Duration::from_millis(ms));
                        boot_log("auto exit requested");
                        handle.exit(0);
                    });
                }
            }

            spawn_spike_driver(app.handle().clone());

            eprintln!("[setup] done");
            Ok(())
        });

    builder
        .build(tauri::generate_context!())
        .expect("error while building tauri application")
        .run(|app, event| match event {
            RunEvent::ExitRequested { .. } => {
                diag::mark_shutdown(app);
            }
            RunEvent::Exit => {
                boot_log("run event: exit");
                // 先让看门狗闭嘴，再回收引擎；否则正常退出会被记成「引擎已退出」。
                diag::mark_shutdown(app);
                if let Some(state) = app.try_state::<ShellState>() {
                    state.engine.stop();
                }
            }
            RunEvent::WindowEvent { label, event, .. } => {
                let what = match &event {
                    tauri::WindowEvent::Destroyed => Some("destroyed"),
                    tauri::WindowEvent::CloseRequested { .. } => Some("close-requested"),
                    _ => None,
                };
                if let Some(what) = what {
                    boot_log(&format!("window {label} {what}"));
                }
            }
            _ => {}
        });
}

fn setup_tray(app: &mut tauri::App) -> tauri::Result<()> {
    // 第一项就是状态：正常时显示「运行正常」，失败时变成「引擎已退出 / N 路断流」，
    // 点它直接打开诊断页。托盘菜单是引擎挂了以后仍然可用的唯一 UI。
    let status = MenuItem::with_id(app, "diag", "… 正在启动引擎", true, None::<&str>)?;
    let export = MenuItem::with_id(app, "diag_export", "导出诊断包…", true, None::<&str>)?;
    let logs = MenuItem::with_id(app, "diag_logs", "打开日志目录", true, None::<&str>)?;
    let sep = PredefinedMenuItem::separator(app)?;
    let show = MenuItem::with_id(app, "show", "显示主窗口", true, None::<&str>)?;
    let stopall = MenuItem::with_id(app, "stopall", "停止全部录音", true, None::<&str>)?;
    // 引擎就绪前先禁用：否则点开控制台只会在 JS 里报「状态不存在」。
    let embedded = MenuItem::with_id(app, "embedded", "应用内浏览器", false, None::<&str>)?;
    let rooms = MenuItem::with_id(app, "rooms", "收听工作区", false, None::<&str>)?;
    // 「在浏览器中打开」保留为兜底：实时监听等依赖真实浏览器标签页的流程走这里。
    let browser = MenuItem::with_id(app, "browser", "在浏览器中打开", false, None::<&str>)?;
    let quit = MenuItem::with_id(app, "quit", "退出 LiveNotes", true, None::<&str>)?;
    let menu = Menu::with_items(
        app,
        &[&status, &export, &logs, &sep, &show, &stopall, &embedded, &rooms, &browser, &quit],
    )?;

    let normal_icon = tauri::image::Image::from_bytes(include_bytes!("../icons/32x32.png"))?;
    let warn_icon = diag::warning_icon(&normal_icon);
    let tray = TrayIconBuilder::with_id("main")
        .tooltip("LiveNotes · 正在启动引擎")
        .icon(normal_icon.clone())
        .menu(&menu)
        .show_menu_on_left_click(false)
        .on_menu_event(|app, event| match event.id().as_ref() {
            "show" => show_main(app),
            "stopall" => {
                show_main(app);
                let _ = app.emit_to("main", "shell_stop_all", ());
            }
            "embedded" => {
                let _ = rooms::open_browser_console(app);
            }
            "rooms" => {
                show_main(app);
                if let Err(e) = app.emit_to("main", "shell_navigate", "/rooms") {
                    boot_log(&format!("tray shell navigate failed: {e}"));
                }
            }
            "browser" => {
                if let Some(state) = app.try_state::<ShellState>() {
                    if let Err(e) = app
                        .opener()
                        .open_url(state.engine.base_url(), None::<&str>)
                    {
                        boot_log(&format!("open browser failed: {e}"));
                    }
                }
            }
            "diag" => {
                let _ = diag::open_window(app);
            }
            "diag_export" => {
                let app = app.clone();
                std::thread::spawn(move || {
                    if let Ok(path) = diag::export(&app) {
                        diag::reveal_file(&app, &path);
                    }
                });
            }
            "diag_logs" => diag::open_logs(app),
            "quit" => {
                let app = app.clone();
                std::thread::spawn(move || quit_app(&app));
            }
            _ => {}
        })
        .on_tray_icon_event(|tray, event| {
            if let TrayIconEvent::DoubleClick { .. } = event {
                show_main(tray.app_handle());
            }
        })
        .build(app)?;
    diag::install_tray(
        app.handle(),
        status,
        embedded,
        rooms,
        browser,
        tray,
        normal_icon,
        warn_icon,
    );
    Ok(())
}

fn show_main(app: &tauri::AppHandle) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.show();
        let _ = window.unminimize();
        let _ = window.set_focus();
    }
}

/// 还有几路在录。托盘「退出」先问这一句：有录音就让壳页弹确认，
/// 没录音才直接退出，避免用户以为关了其实还在录。
fn active_rooms(app: &tauri::AppHandle) -> usize {
    let Some(state) = app.try_state::<ShellState>() else {
        return 0;
    };
    let Ok(r) = http::get(
        state.engine.port,
        "/api/rooms",
        std::time::Duration::from_millis(1200),
    ) else {
        return 0;
    };
    if r.status != 200 {
        return 0;
    }
    let Ok(v) = r.json() else {
        return 0;
    };
    v.get("rooms")
        .and_then(|x| x.as_array())
        .map(|rows| {
            rows.iter()
                .filter(|row| {
                    matches!(
                        row.get("state").and_then(|s| s.as_str()),
                        Some("pending" | "starting" | "live")
                    )
                })
                .count()
        })
        .unwrap_or(0)
}

fn quit_app(app: &tauri::AppHandle) {
    let n = active_rooms(app);
    if n > 0 {
        show_main(app);
        let _ = app.emit_to("main", "shell_quit_requested", n);
    } else {
        app.exit(0);
    }
}

/// 壳页收到关闭请求、准备弹确认时先回执，避免 2 秒兜底把窗口藏掉。
#[tauri::command]
fn shell_close_ack() {
    ack_close();
}

/// 壳页决定「最小化到托盘」后调用；不直接关窗，避免和 CloseRequested 抢。
#[tauri::command]
fn shell_hide(app: tauri::AppHandle) {
    ack_close();
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.hide();
    }
}

/// 壳页确认退出后调用；走正常 Exit 事件回收引擎。
#[tauri::command]
fn shell_quit(app: tauri::AppHandle) {
    ack_close();
    app.exit(0);
}

/// D2/D3/D4 自动化：打开控制台，按页面真实的按钮路径操作。
/// 只有设置 LIVENOTES_SPIKE=d2|d3|d4|browser 时才运行，不影响正常使用。
fn spawn_spike_driver(app: tauri::AppHandle) {
    let Ok(mode) = std::env::var("LIVENOTES_SPIKE") else {
        return;
    };
    boot_log(&format!("spike: driver mode={mode}"));
    std::thread::spawn(move || {
        std::thread::sleep(std::time::Duration::from_millis(1500));
        // 打开设置抽屉，供截图确认桌面专用选项真的显示出来。
        if mode == "settingsui" {
            std::thread::sleep(std::time::Duration::from_secs(3));
            let Some(main) = app.get_webview_window("main") else {
                boot_log("spike settingsui: main window missing");
                return;
            };
            let _ = main.set_focus();
            spike_eval(&main, "document.getElementById('btnSettings').click()", "settingsui-open");
            std::thread::sleep(std::time::Duration::from_secs(2));
            spike_eval(
                &main,
                "document.getElementById('prefOpenModeRow').scrollIntoView({block:'center'})",
                "settingsui-scroll",
            );
            std::thread::sleep(std::time::Duration::from_secs(10));
            return;
        }
        // M1 本地壳：本地 origin + iframe 引擎页，验证 route (b)。
        if mode == "m1local" {
            std::thread::sleep(std::time::Duration::from_secs(6));
            if let Some(main) = app.get_webview_window("main") {
                let _ = main.set_focus();
            }
            boot_log("m1local: state=shell-visible");
            std::thread::sleep(std::time::Duration::from_secs(12));
            boot_log("m1local: state=child-hidden");
            std::thread::sleep(std::time::Duration::from_secs(10));
            boot_log("m1local: state=child-shown");
            std::thread::sleep(std::time::Duration::from_secs(8));
            boot_log("m1local: done");
            return;
        }
        // M1 正式壳验收：通过页面里的最小控制面逐页切换，打点交给外部截图。
        if mode == "m1shell" {
            let Some(main) = app.get_webview_window("main") else {
                boot_log("m1shell: main window missing");
                return;
            };
            let _ = main.set_focus();
            std::thread::sleep(std::time::Duration::from_secs(9));
            spike_eval(
                &main,
                "window.__LN_SHELL__ && window.__LN_SHELL__.select(0)",
                "shell-select",
            );
            std::thread::sleep(std::time::Duration::from_secs(3));
            spike_eval(
                &main,
                "window.__LN_SHELL__ && window.__LN_SHELL__.report('m1shell-listen')",
                "shell-report-listen",
            );
            boot_log("m1shell: state=listen");
            std::thread::sleep(std::time::Duration::from_secs(5));

            spike_eval(
                &main,
                "window.__LN_SHELL__ && window.__LN_SHELL__.wall()",
                "shell-wall",
            );
            std::thread::sleep(std::time::Duration::from_secs(5));
            boot_log("m1shell: state=wall");
            std::thread::sleep(std::time::Duration::from_secs(5));

            spike_eval(
                &main,
                "window.__LN_SHELL__ && window.__LN_SHELL__.palette()",
                "shell-palette",
            );
            std::thread::sleep(std::time::Duration::from_secs(5));
            boot_log("m1shell: state=palette");
            std::thread::sleep(std::time::Duration::from_secs(5));

            spike_eval(
                &main,
                "(function(){var s=window.__LN_SHELL__;if(!s)return;s.paletteClose();s.pageUrl(window.__LN_ENTRY__+'rooms');return s.page();})()",
                "shell-page",
            );
            std::thread::sleep(std::time::Duration::from_secs(5));
            boot_log("m1shell: state=page");
            std::thread::sleep(std::time::Duration::from_secs(5));

            spike_eval(
                &main,
                "window.__LN_SHELL__ && window.__LN_SHELL__.pageClose()",
                "shell-page-close",
            );
            std::thread::sleep(std::time::Duration::from_secs(5));
            boot_log("m1shell: state=page-closed");
            std::thread::sleep(std::time::Duration::from_secs(5));

            spike_eval(
                &main,
                "window.__LN_SHELL__ && window.__LN_SHELL__.workbench()",
                "shell-workbench",
            );
            std::thread::sleep(std::time::Duration::from_secs(5));
            boot_log("m1shell: state=workbench");
            std::thread::sleep(std::time::Duration::from_secs(5));

            spike_eval(
                &main,
                "window.__LN_SHELL__ && window.__LN_SHELL__.view('files')",
                "shell-files",
            );
            std::thread::sleep(std::time::Duration::from_secs(4));
            boot_log("m1shell: state=files");
            std::thread::sleep(std::time::Duration::from_secs(5));

            spike_eval(
                &main,
                "window.__LN_SHELL__ && window.__LN_SHELL__.view('people')",
                "shell-people",
            );
            std::thread::sleep(std::time::Duration::from_secs(4));
            boot_log("m1shell: state=people");
            std::thread::sleep(std::time::Duration::from_secs(5));

            spike_eval(
                &main,
                "window.__LN_SHELL__ && window.__LN_SHELL__.view('settings')",
                "shell-settings",
            );
            std::thread::sleep(std::time::Duration::from_secs(4));
            boot_log("m1shell: state=settings");
            std::thread::sleep(std::time::Duration::from_secs(5));

            spike_eval(
                &main,
                "window.__LN_SHELL__ && window.__LN_SHELL__.report('m1shell-final')",
                "shell-report-final",
            );
            boot_log("m1shell: done");
            return;
        }
        // M1 单窗口壳：主窗口 IPC、子视图挂载/布局、原生视图 z 序。
        // 房间操作直接从 Rust 调 rooms::，不依赖 IPC，这样 IPC 失败也能单独判断
        // “子视图挂载”是否成立；IPC 自己由 spike_report 与 URL hash 双通道取证。
        if mode == "m1" {
            let Some(main) = app.get_webview_window("main") else {
                boot_log("m1: main window missing");
                return;
            };
            let _ = main.set_focus();
            std::thread::sleep(std::time::Duration::from_secs(2));

            boot_log("m1: ipc probe");
            spike_eval(&main, M1_IPC, "m1-ipc");
            std::thread::sleep(std::time::Duration::from_secs(4));
            match main.url() {
                Ok(u) => boot_log(&format!("m1: main url {u}")),
                Err(e) => boot_log(&format!("m1: main url failed: {e}")),
            }

            boot_log("m1: overlay on");
            spike_eval(&main, M1_OVERLAY, "m1-overlay-on");
            std::thread::sleep(std::time::Duration::from_secs(1));

            let port = app.state::<rooms::RoomState>().port;
            let url = format!("http://127.0.0.1:{port}/static/spike-stage.html");
            boot_log(&format!("m1: open child {url}"));
            let opened = tauri::async_runtime::block_on(rooms::room_open(
                app.get_window("main").unwrap(),
                app.clone(),
                app.state::<rooms::RoomState>(),
                "m1-stage".to_string(),
                url,
                String::new(),
                Some(false),
                None,
                360.0,
                140.0,
                760.0,
                520.0,
            ));
            match opened {
                Ok(()) => boot_log("m1: child open ok"),
                Err(e) => boot_log(&format!("m1: child open failed: {e}")),
            }
            std::thread::sleep(std::time::Duration::from_secs(8));
            boot_log("m1: state=child-visible");
            std::thread::sleep(std::time::Duration::from_secs(10));

            let hidden = rooms::room_show(
                app.state::<rooms::RoomState>(),
                "m1-stage".to_string(),
                false,
            );
            match hidden {
                Ok(()) => boot_log("m1: child hide ok"),
                Err(e) => boot_log(&format!("m1: child hide failed: {e}")),
            }
            std::thread::sleep(std::time::Duration::from_secs(2));
            boot_log("m1: state=child-hidden");
            std::thread::sleep(std::time::Duration::from_secs(10));

            let moved = rooms::room_layout(
                app.state::<rooms::RoomState>(),
                "m1-stage".to_string(),
                820.0,
                300.0,
                520.0,
                360.0,
            )
            .and_then(|()| {
                rooms::room_show(
                    app.state::<rooms::RoomState>(),
                    "m1-stage".to_string(),
                    true,
                )
            });
            match moved {
                Ok(()) => boot_log("m1: child move+show ok"),
                Err(e) => boot_log(&format!("m1: child move+show failed: {e}")),
            }
            std::thread::sleep(std::time::Duration::from_secs(2));
            boot_log("m1: state=child-moved");
            std::thread::sleep(std::time::Duration::from_secs(10));

            let closed = rooms::room_close(
                app.state::<rooms::RoomState>(),
                "m1-stage".to_string(),
            );
            match closed {
                Ok(()) => boot_log("m1: child close ok"),
                Err(e) => boot_log(&format!("m1: child close failed: {e}")),
            }
            std::thread::sleep(std::time::Duration::from_secs(2));
            boot_log("m1: state=child-closed");
            std::thread::sleep(std::time::Duration::from_secs(8));
            boot_log("m1: done");
            return;
        }
        // 验证主窗口 window.open 的路由：不经过控制台，直接点网页里的辅助页。
        if mode == "openmode" {
            std::thread::sleep(std::time::Duration::from_secs(3));
            let Some(main) = app.get_webview_window("main") else {
                boot_log("spike openmode: main window missing");
                return;
            };
            spike_eval(&main, "window.open('/rooms','_blank')", "openmode-rooms");
            std::thread::sleep(std::time::Duration::from_secs(8));
            spike_eval(&main, "window.open('/library','_blank')", "openmode-library");
            std::thread::sleep(std::time::Duration::from_secs(8));
            return;
        }
        let (tx, rx) = std::sync::mpsc::channel();
        let console_app = app.clone();
        let embedded = mode == "browser" || mode == "live";
        if let Err(e) = app.run_on_main_thread(move || {
            let window = if embedded {
                rooms::open_browser_console(&console_app)
            } else {
                rooms::open_rooms_console(&console_app)
            };
            let _ = tx.send(window);
        }) {
            boot_log(&format!("spike: open console failed: {e}"));
            return;
        }
        let console = match rx.recv_timeout(std::time::Duration::from_secs(15)) {
            Ok(Some(console)) => console,
            Ok(None) => {
                boot_log("spike: console window not created");
                return;
            }
            Err(e) => {
                boot_log(&format!("spike: console wait failed: {e}"));
                return;
            }
        };
        std::thread::sleep(std::time::Duration::from_millis(2500));

        match mode.as_str() {
            // 应用内浏览器：从地址栏的真实路径开两个标签页，验证采集链路。
            "browser" => {
                std::thread::sleep(std::time::Duration::from_millis(1500));
                spike_eval(
                    &console,
                    "window.__LN_TEST_OPEN('http://127.0.0.1:8899/room/a')",
                    "browser-open-a",
                );
                std::thread::sleep(std::time::Duration::from_secs(30));
                spike_eval(
                    &console,
                    "window.__LN_TEST_OPEN('http://127.0.0.1:8899/room/b')",
                    "browser-open-b",
                );
                std::thread::sleep(std::time::Duration::from_secs(30));
            }
            // 实时字幕验收：开 A 出字后点「字幕」，确认字幕窗口真的加载出页面、
            // 而且壳的主线程没被建窗口卡住（同步命令的卡死会在这里超时）。
            "live" => {
                spike_eval(
                    &console,
                    "window.__LN_TEST_OPEN('http://127.0.0.1:8899/room/a')",
                    "live-open-a",
                );
                std::thread::sleep(std::time::Duration::from_secs(12));
                spike_eval(&console, "window.__LN_TEST_OPEN_LIVE()", "live-open");
                std::thread::sleep(std::time::Duration::from_secs(8));

                let (ready_tx, ready_rx) = std::sync::mpsc::channel();
                if app
                    .run_on_main_thread(move || {
                        let _ = ready_tx.send(());
                    })
                    .is_err()
                {
                    boot_log("live: run_on_main_thread failed");
                }
                match ready_rx.recv_timeout(std::time::Duration::from_secs(5)) {
                    Ok(()) => boot_log("live: main thread responsive"),
                    Err(e) => boot_log(&format!("live: main thread blocked: {e}")),
                }
                let labels: Vec<String> = app.webview_windows().keys().cloned().collect();
                boot_log(&format!("live: windows={labels:?}"));
                for label in labels.iter().filter(|l| l.starts_with("live-")) {
                    if let Some(w) = app.get_webview_window(label) {
                        let _ = w.close();
                    }
                }
                std::thread::sleep(std::time::Duration::from_secs(3));
                let (done_tx, done_rx) = std::sync::mpsc::channel();
                let _ = app.run_on_main_thread(move || {
                    let _ = done_tx.send(());
                });
                match done_rx.recv_timeout(std::time::Duration::from_secs(5)) {
                    Ok(()) => boot_log("live: main thread responsive after close"),
                    Err(e) => boot_log(&format!("live: main thread blocked after close: {e}")),
                }
            }
            "d2" => {
                spike_eval(&console, "document.getElementById('btn-open-a').click()", "open-a");
                std::thread::sleep(std::time::Duration::from_secs(8));
                spike_eval(&console, "document.getElementById('btn-open-b').click()", "open-b");
                std::thread::sleep(std::time::Duration::from_secs(14));
                spike_eval(&console, "document.getElementById('btn-close-a').click()", "close-a");
                std::thread::sleep(std::time::Duration::from_secs(6));
                spike_eval(&console, "document.getElementById('btn-close-b').click()", "close-b");
            }
            "d3" => {
                spike_eval(&console, "document.getElementById('btn-open-a').click()", "open-a");
                std::thread::sleep(std::time::Duration::from_secs(18));
                spike_eval(&console, "document.getElementById('btn-mute-a').click()", "mute-a");
                std::thread::sleep(std::time::Duration::from_secs(55));
            }
            // 诊断验收：开 A → 让渲染进程真崩一次 → 关掉 A 制造断流。
            // 后面的“杀引擎”由 desktop/test/verify_diag.py 从外部完成。
            "d3crash" => {
                spike_eval(&console, "document.getElementById('btn-open-a').click()", "open-a");
                std::thread::sleep(std::time::Duration::from_secs(18));
                boot_log("d3crash: Page.crash room-a");
                match rooms::crash_room_for_test(&app, "room-a") {
                    Ok(()) => boot_log("d3crash: Page.crash sent"),
                    Err(e) => boot_log(&format!("d3crash: Page.crash failed: {e}")),
                }
                std::thread::sleep(std::time::Duration::from_secs(10));
                boot_log("d3crash: closing room-a to stop chunks");
                spike_eval(&console, "document.getElementById('btn-close-a').click()", "close-a");
                std::thread::sleep(std::time::Duration::from_secs(40));
            }
            "d4" => {
                spike_eval(&console, D4_OPEN, "d4-open-3");
                std::thread::sleep(std::time::Duration::from_secs(35));

                let minimize_secs = env_u64("LIVENOTES_D4_MINIMIZE_SECS", 600);
                boot_log(&format!("d4: minimize console for {minimize_secs}s"));
                let _ = console.minimize();
                std::thread::sleep(std::time::Duration::from_secs(minimize_secs));
                boot_log("d4: restore console");
                let _ = console.unminimize();
                let _ = console.show();
                let _ = console.set_focus();

                let rebuild_at = env_u64("LIVENOTES_D4_REBUILD_SECS", 1500);
                let now_at = 35 + minimize_secs;
                if rebuild_at > now_at {
                    std::thread::sleep(std::time::Duration::from_secs(rebuild_at - now_at));
                }
                boot_log("d4: rebuild room-c");
                spike_eval(&console, D4_REBUILD, "d4-rebuild-c");
            }
            other => boot_log(&format!("spike: unknown mode {other:?}")),
        }
    });
}

fn env_u64(name: &str, default: u64) -> u64 {
    std::env::var(name)
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(default)
}

#[tauri::command]
fn spike_report(tag: String, detail: Option<String>) {
    let detail = detail
        .unwrap_or_default()
        .replace('\r', " ")
        .replace('\n', " ");
    boot_log(&format!("spike-report tag={tag} detail={detail}"));
}

fn spike_eval(console: &tauri::WebviewWindow, js: &str, tag: &str) {
    match console.eval(js) {
        Ok(()) => boot_log(&format!("spike: eval {tag}")),
        Err(e) => boot_log(&format!("spike: eval {tag} failed: {e}")),
    }
}

/// 主窗口 IPC 探测：三条证据一次拿全。
/// 1) `__TAURI__` / `__TAURI_INTERNALS__` 是否存在（withGlobalTauri + IPC 注入）；
/// 2) 两个 invoke 入口是否被 capability 放行（成功会到 Rust 的 spike-report）；
/// 3) 把结果写进 URL hash，即使 IPC 全挂也能从 Rust 侧读到错误文本。
const M1_IPC: &str = r#"
(function () {
  const r = {};
  const finish = function () {
    try { location.hash = 'spike-ipc=' + encodeURIComponent(JSON.stringify(r)); } catch (e) {}
    try { document.title = 'M1 IPC ' + JSON.stringify(r); } catch (e) {}
  };
  (async function () {
    r.tauri = typeof window.__TAURI__;
    r.internals = typeof window.__TAURI_INTERNALS__;
    try {
      if (window.__TAURI__ && window.__TAURI__.core && window.__TAURI__.core.invoke) {
        await window.__TAURI__.core.invoke('spike_report', {
          tag: 'm1-ipc-tauri',
          detail: JSON.stringify({ origin: location.origin })
        });
        r.tauriInvoke = 'ok';
      } else {
        r.tauriInvoke = 'missing';
      }
    } catch (e) {
      r.tauriInvoke = 'err:' + String(e).slice(0, 160);
    }
    try {
      if (window.__TAURI_INTERNALS__ && window.__TAURI_INTERNALS__.invoke) {
        await window.__TAURI_INTERNALS__.invoke('spike_report', {
          tag: 'm1-ipc-internals', detail: 'ok'
        });
        r.internalsInvoke = 'ok';
      } else {
        r.internalsInvoke = 'missing';
      }
    } catch (e) {
      r.internalsInvoke = 'err:' + String(e).slice(0, 160);
    }
    finish();
  })();
})()
"#;

/// 模拟命令面板/浮层：全屏压暗 + 中央卡片。卡片正好落在子视图矩形里；
/// 子视图可见时它会“消失”，这是原生视图永远盖在 HTML 之上的直观证据。
const M1_OVERLAY: &str = r#"
(function () {
  if (document.getElementById('spike-overlay')) return;
  const d = document.createElement('div');
  d.id = 'spike-overlay';
  d.style.cssText = [
    'position:fixed', 'inset:0', 'z-index:2147483647',
    'background:rgba(8,10,14,.72)',
    'display:flex', 'align-items:center', 'justify-content:center',
    'pointer-events:none', 'font:600 26px/1.5 "Microsoft YaHei",sans-serif',
    'color:#E6B566', 'letter-spacing:2px'
  ].join(';');
  const card = document.createElement('div');
  card.textContent = 'HTML OVERLAY · 命令面板模拟';
  card.style.cssText = [
    'border:2px solid #E6B566', 'border-radius:14px',
    'background:#12161B', 'padding:26px 38px',
    'box-shadow:0 24px 80px rgba(0,0,0,.55)'
  ].join(';');
  d.appendChild(card);
  document.body.appendChild(d);
  try { location.hash = 'spike-overlay=on'; } catch (e) {}
})()
"#;

const D4_OPEN: &str = r#"
(function () {
  const inv = window.__TAURI__.core.invoke;
  const W = window.innerWidth, H = window.innerHeight;
  const y = 132, gap = 6;
  const w = Math.floor((W - gap * 4) / 3);
  const h = H - y - gap;
  window.__D4 = {};
  const rooms = [
    ['room-a', 'a', gap],
    ['room-b', 'b', gap * 2 + w],
    ['room-c', 'c', gap * 3 + w * 2]
  ];
  (async () => {
    for (const item of rooms) {
      const label = item[0], room = item[1], x = item[2];
      const sid = await inv('live_start', { title: 'D4 ' + room.toUpperCase() });
      window.__D4[room] = sid;
      await inv('room_open', {
        label: label,
        url: 'http://127.0.0.1:8899/room/' + room,
        sid: sid, x: x, y: y, w: w, h: h
      });
    }
  })().catch(function (e) { console.error('D4 open failed', e); });
})()
"#;

const D4_REBUILD: &str = r#"
(function () {
  const inv = window.__TAURI__.core.invoke;
  const W = window.innerWidth, H = window.innerHeight;
  const y = 132, gap = 6;
  const w = Math.floor((W - gap * 4) / 3);
  const h = H - y - gap;
  (async () => {
    await inv('room_close', { label: 'room-c' });
    await new Promise(function (r) { setTimeout(r, 1500); });
    await inv('room_open', {
      label: 'room-c', url: 'http://127.0.0.1:8899/room/c',
      sid: (window.__D4 && window.__D4.c) || '',
      x: gap * 3 + w * 2, y: y, w: w, h: h
    });
  })().catch(function (e) { console.error('D4 rebuild failed', e); });
})()
"#;
