//! 桌面故障诊断：托盘状态、引擎/断流/房间视图看门狗、诊断包导出。
//!
//! 设计原则和 docs/21 一致：
//! - 失败必须留下机器可读的一条（``logs/diag.jsonl``）和人能看懂的一条
//!   （``logs/shell.log``），不能只靠 stderr —— GUI 启动方式会把它丢掉；
//! - 托盘是桌面用户唯一一直存在的入口：状态项、tooltip、告警图标三者一起变，
//!   不让“引擎已经退出”只体现在一个死掉的页面上；
//! - 导出诊断包不依赖引擎存活：日志、事件、WebView2 崩溃转储都直接读文件，
//!   HTTP 快照拿不到就写一条 error，包照样能生成；
//! - 不收集 settings.json、LLM Key、录音和字幕正文。

use std::collections::{HashMap, HashSet, VecDeque};
use std::fs::{self, File, OpenOptions};
use std::io::{Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};
use std::process::Child;
use std::sync::atomic::{AtomicBool, AtomicU32, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use serde::Serialize;
use serde_json::{json, Map, Value};
use tauri::image::Image;
use tauri::menu::MenuItem;
use tauri::tray::TrayIcon;
use tauri::{AppHandle, Emitter, Manager, State, WebviewUrl, WebviewWindowBuilder, Wry};
use tauri_plugin_opener::OpenerExt;
use zip::write::SimpleFileOptions;
use zip::{CompressionMethod, ZipWriter};

use crate::engine;
use crate::http;

const MAX_EVENTS: usize = 240;
const DIAG_LOG_MAX_BYTES: u64 = 2 * 1024 * 1024;
const TAIL_BYTES: u64 = 8 * 1024 * 1024;
const STALL_DEFAULT_S: f64 = 30.0;
const RECENT_CRASH_S: u64 = 600;

#[derive(Clone, Serialize)]
pub struct DiagEvent {
    pub ts: String,
    pub epoch: u64,
    pub level: String,
    pub kind: String,
    pub message: String,
    pub detail: Value,
}

#[derive(Clone, Default)]
pub struct EngineStatus {
    pub state: String,
    pub port: u16,
    pub pid: u32,
    pub started_at: u64,
    pub detail: String,
    pub since_epoch: u64,
    pub last_ok_epoch: u64,
    pub failures: u32,
}

struct StallInfo {
    sid: String,
    title: String,
    idle_s: f64,
    notified: bool,
    last_seen: Instant,
    missing_polls: u32,
}

struct CrashInfo {
    room: String,
    kind: String,
    detail: String,
    epoch: u64,
}

struct TrayUi {
    status: MenuItem<Wry>,
    embedded_item: MenuItem<Wry>,
    rooms_item: MenuItem<Wry>,
    browser_item: MenuItem<Wry>,
    tray: TrayIcon<Wry>,
    normal_icon: Image<'static>,
    warn_icon: Option<Image<'static>>,
    warned: bool,
}

pub struct Diag {
    events: Mutex<VecDeque<DiagEvent>>,
    engine: Mutex<EngineStatus>,
    stalls: Mutex<HashMap<String, StallInfo>>,
    crashes: Mutex<VecDeque<CrashInfo>>,
    tray: Mutex<Option<TrayUi>>,
    last_export: Mutex<Option<(PathBuf, u64)>>,
    flash: Mutex<Option<(String, Instant)>>,
    stall_threshold: Mutex<f64>,
    threshold_pinned: bool,
    export_lock: Mutex<()>,
    shutdown: AtomicBool,
    /// 测试开关 LIVENOTES_DIAG_AUTO_EXPORT：只为自动化验证同一套导出路径，
    /// 正常使用不设置，导出完全由托盘/按钮触发。
    auto_exports: AtomicU32,
    last_auto_export: Mutex<Option<Instant>>,
}

impl Diag {
    pub fn new() -> Self {
        let threshold = std::env::var("LIVENOTES_SHELL_STALL_SECONDS")
            .ok()
            .and_then(|v| v.parse::<f64>().ok())
            .filter(|v| *v > 0.0)
            .unwrap_or(STALL_DEFAULT_S);
        let pinned = std::env::var("LIVENOTES_SHELL_STALL_SECONDS").is_ok();
        Self {
            events: Mutex::new(VecDeque::new()),
            engine: Mutex::new(EngineStatus {
                state: "starting".into(),
                ..Default::default()
            }),
            stalls: Mutex::new(HashMap::new()),
            crashes: Mutex::new(VecDeque::new()),
            tray: Mutex::new(None),
            last_export: Mutex::new(None),
            flash: Mutex::new(None),
            stall_threshold: Mutex::new(threshold),
            threshold_pinned: pinned,
            export_lock: Mutex::new(()),
            shutdown: AtomicBool::new(false),
            auto_exports: AtomicU32::new(0),
            last_auto_export: Mutex::new(None),
        }
    }
}

fn now_epoch() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0)
}

/// UTC ISO8601，不引入 chrono；事件顺序按 epoch 排，显示按 UTC 最省事。
fn iso_utc(epoch: u64) -> String {
    let days = (epoch / 86400) as i64;
    let secs = epoch % 86400;
    let (y, m, d) = civil_from_days(days);
    format!(
        "{:04}-{:02}-{:02}T{:02}:{:02}:{:02}Z",
        y,
        m,
        d,
        secs / 3600,
        (secs % 3600) / 60,
        secs % 60
    )
}

fn compact_utc(epoch: u64) -> String {
    let days = (epoch / 86400) as i64;
    let secs = epoch % 86400;
    let (y, m, d) = civil_from_days(days);
    format!(
        "{:04}{:02}{:02}-{:02}{:02}{:02}Z",
        y,
        m,
        d,
        secs / 3600,
        (secs % 3600) / 60,
        secs % 60
    )
}

fn civil_from_days(z: i64) -> (i64, u32, u32) {
    let z = z + 719468;
    let era = if z >= 0 { z } else { z - 146096 } / 146097;
    let doe = (z - era * 146097) as u64;
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
    let y = yoe as i64 + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = (doy - (153 * mp + 2) / 5 + 1) as u32;
    let m = if mp < 10 { mp + 3 } else { mp - 9 } as u32;
    (if m <= 2 { y + 1 } else { y }, m, d)
}

/// 在托盘图标右上角画一个红底白感叹号。没有这个角标时，失败只体现在
/// tooltip 和菜单文字里，最小化的窗口不会给用户任何视觉信号。
pub fn warning_icon(base: &Image<'_>) -> Option<Image<'static>> {
    let (w, h) = (base.width(), base.height());
    if w < 10 || h < 10 {
        return None;
    }
    let mut px = base.rgba().to_vec();
    if px.len() < (w as usize) * (h as usize) * 4 {
        return None;
    }
    let r = ((w.min(h) as f32) * 0.40).round().max(4.0) as i32;
    let cx = w as i32 - r - 1;
    let cy = r + 1;
    let mut put = |x: i32, y: i32, c: [u8; 4]| {
        if x < 0 || y < 0 || x >= w as i32 || y >= h as i32 {
            return;
        }
        let idx = ((y as usize) * (w as usize) + x as usize) * 4;
        px[idx..idx + 4].copy_from_slice(&c);
    };
    for y in 0..h as i32 {
        for x in 0..w as i32 {
            let dx = x - cx;
            let dy = y - cy;
            let d2 = dx * dx + dy * dy;
            if d2 <= r * r {
                if d2 > (r - 2).max(1) * (r - 2).max(1) {
                    put(x, y, [255, 255, 255, 255]);
                } else {
                    put(x, y, [204, 51, 43, 255]);
                }
            }
        }
    }
    let bar_w = (r / 3).max(1);
    let bar_top = cy - r / 2;
    let bar_bottom = cy + r / 4;
    for y in bar_top..=bar_bottom {
        for x in (cx - bar_w)..=(cx + bar_w) {
            put(x, y, [255, 255, 255, 255]);
        }
    }
    for y in (cy + r / 2 - 1)..=(cy + r / 2) {
        for x in (cx - bar_w)..=(cx + bar_w) {
            put(x, y, [255, 255, 255, 255]);
        }
    }
    drop(put);
    Some(Image::new_owned(px, w, h))
}

pub fn is_shutdown(app: &AppHandle) -> bool {
    app.try_state::<Diag>()
        .map(|d| d.shutdown.load(Ordering::Relaxed))
        .unwrap_or(true)
}

pub fn mark_shutdown(app: &AppHandle) {
    if let Some(d) = app.try_state::<Diag>() {
        d.shutdown.store(true, Ordering::Relaxed);
    }
}

// ---------------------------------------------------------------- 事件与托盘

pub fn record(app: &AppHandle, level: &str, kind: &str, message: &str, detail: Value) {
    let epoch = now_epoch();
    let ev = DiagEvent {
        ts: iso_utc(epoch),
        epoch,
        level: level.to_string(),
        kind: kind.to_string(),
        message: message.to_string(),
        detail,
    };
    if let Some(d) = app.try_state::<Diag>() {
        let mut q = d.events.lock().unwrap_or_else(|e| e.into_inner());
        q.push_back(ev.clone());
        while q.len() > MAX_EVENTS {
            q.pop_front();
        }
    }

    if let Ok(root) = engine::app_data_root() {
        let logs = root.join("logs");
        let detail_s = serde_json::to_string(&ev.detail).unwrap_or_default();
        let detail_short: String = detail_s.chars().take(600).collect();
        engine::append_log(
            &logs.join("shell.log"),
            &format!(
                "diag level={} kind={} msg={} detail={}",
                ev.level, ev.kind, ev.message, detail_short
            ),
        );
        append_jsonl(&logs.join("diag.jsonl"), &ev);
    }

    refresh_tray(app);
    if level == "error" {
        request_attention(app);
    }
    maybe_auto_export(app, level);
}

fn append_jsonl(path: &Path, ev: &DiagEvent) {
    let Ok(line) = serde_json::to_string(ev) else {
        return;
    };
    if let Some(parent) = path.parent() {
        let _ = fs::create_dir_all(parent);
    }
    if fs::metadata(path)
        .map(|m| m.len() >= DIAG_LOG_MAX_BYTES)
        .unwrap_or(false)
    {
        let rotated = path.with_extension("jsonl.1");
        let _ = fs::rename(path, rotated);
    }
    if let Ok(mut f) = OpenOptions::new().create(true).append(true).open(path) {
        let _ = writeln!(f, "{line}");
    }
}

fn request_attention(app: &AppHandle) {
    let app = app.clone();
    let _ = app.clone().run_on_main_thread(move || {
        if let Some(w) = app.get_webview_window("main") {
            if !w.is_focused().unwrap_or(false) {
                let _ = w.request_user_attention(Some(tauri::UserAttentionType::Informational));
            }
        }
    });
}

pub fn install_tray(
    app: &AppHandle,
    status: MenuItem<Wry>,
    embedded_item: MenuItem<Wry>,
    rooms_item: MenuItem<Wry>,
    browser_item: MenuItem<Wry>,
    tray: TrayIcon<Wry>,
    normal_icon: Image<'static>,
    warn_icon: Option<Image<'static>>,
) {
    let Some(d) = app.try_state::<Diag>() else {
        return;
    };
    *d.tray.lock().unwrap_or_else(|e| e.into_inner()) = Some(TrayUi {
        status,
        embedded_item,
        rooms_item,
        browser_item,
        tray,
        normal_icon,
        warn_icon,
        warned: false,
    });
    refresh_tray(app);
}

struct StatusView {
    text: String,
    tooltip: String,
    warn: bool,
}

fn compute_status(d: &Diag) -> StatusView {
    let engine_state = d
        .engine
        .lock()
        .unwrap_or_else(|e| e.into_inner())
        .state
        .clone();
    let stall_count = d
        .stalls
        .lock()
        .unwrap_or_else(|e| e.into_inner())
        .values()
        .filter(|s| s.notified)
        .count();
    let now = now_epoch();
    let crash_count = d
        .crashes
        .lock()
        .unwrap_or_else(|e| e.into_inner())
        .iter()
        .filter(|c| now.saturating_sub(c.epoch) <= RECENT_CRASH_S)
        .count();
    let flash = d.flash.lock().unwrap_or_else(|e| e.into_inner()).clone();
    status_view(&engine_state, stall_count, crash_count, flash.as_ref())
}

/// 托盘状态的纯函数：测试不需要构造 `Diag`（它会拖进 MenuItem/TrayIcon 的
/// drop glue，而单元测试可执行文件没有 Common-Controls v6 manifest）。
fn status_view(
    engine_state: &str,
    stall_count: usize,
    crash_count: usize,
    flash: Option<&(String, Instant)>,
) -> StatusView {
    let flash_active = flash
        .map(|(_, at)| at.elapsed() < Duration::from_secs(8))
        .unwrap_or(false);

    let (mut text, mut tooltip) = if !matches!(engine_state, "ok") {
        match engine_state {
            "starting" => (
                "… 正在启动引擎".to_string(),
                "LiveNotes · 正在启动引擎".to_string(),
            ),
            "unreachable" => (
                "⚠ 引擎无响应 · 打开诊断".to_string(),
                "LiveNotes · 引擎无响应（右键托盘导出诊断）".to_string(),
            ),
            "exited" => (
                "✖ 引擎已退出 · 打开诊断".to_string(),
                "LiveNotes · 引擎已退出（右键托盘导出诊断）".to_string(),
            ),
            _ => (
                "✖ 引擎未启动 · 打开诊断".to_string(),
                "LiveNotes · 引擎未启动（右键托盘导出诊断）".to_string(),
            ),
        }
    } else if stall_count > 0 {
        (
            format!("⚠ {stall_count} 路断流 · 打开诊断"),
            format!("LiveNotes · {stall_count} 路断流（右键托盘导出诊断）"),
        )
    } else if crash_count > 0 {
        (
            "⚠ 房间视图异常 · 打开诊断".to_string(),
            "LiveNotes · 房间视图异常（已尝试自动重载）".to_string(),
        )
    } else {
        (
            "✓ 运行正常 · 打开诊断".to_string(),
            "LiveNotes · 运行正常".to_string(),
        )
    };
    if flash_active {
        if let Some((msg, _)) = flash {
            text = msg.clone();
            tooltip = format!("LiveNotes · {msg}");
        }
    }
    StatusView {
        text,
        tooltip,
        warn: !matches!(engine_state, "ok") || stall_count > 0 || crash_count > 0,
    }
}

pub fn refresh_tray(app: &AppHandle) {
    let Some(d) = app.try_state::<Diag>() else {
        return;
    };
    let view = compute_status(&d);
    let engine_ok = d
        .engine
        .lock()
        .unwrap_or_else(|e| e.into_inner())
        .state
        == "ok";
    let handles = {
        let mut guard = d.tray.lock().unwrap_or_else(|e| e.into_inner());
        let Some(tray) = guard.as_mut() else {
            return;
        };
        let changed = tray.warned != view.warn;
        tray.warned = view.warn;
        (
            tray.status.clone(),
            tray.embedded_item.clone(),
            tray.browser_item.clone(),
            tray.tray.clone(),
            tray.normal_icon.clone(),
            tray.warn_icon.clone(),
            changed,
            view.warn,
        )
    };
    let (status, embedded, browser, tray, normal, warn_icon, changed, warn) = handles;
    let text = view.text.clone();
    let tooltip = view.tooltip.clone();
    let app2 = app.clone();
    let _ = app.clone().run_on_main_thread(move || {
        let _ = status.set_text(&text);
        // 引擎没起来时打开的会是一个连不上的地址/空控制台，和「多房间采集」一起置灰。
        let _ = embedded.set_enabled(engine_ok);
        let _ = browser.set_enabled(engine_ok);
        let _ = tray.set_tooltip(Some(tooltip.as_str()));
        if changed {
            let icon = if warn {
                warn_icon.unwrap_or(normal)
            } else {
                normal
            };
            let _ = tray.set_icon(Some(icon));
        }
        // 让诊断页的状态项在托盘点开之前也能刷新；命令本身是拉取式的，
        // 这里只是顺带触发一次前端 refresh（页面没开时没有监听者，无害）。
        let _ = app2.emit("livenotes://diag", ());
    });
}

pub fn flash_status(app: &AppHandle, text: String) {
    if let Some(d) = app.try_state::<Diag>() {
        *d.flash.lock().unwrap_or_else(|e| e.into_inner()) = Some((text, Instant::now()));
        refresh_tray(app);
        let app2 = app.clone();
        std::thread::spawn(move || {
            std::thread::sleep(Duration::from_secs(9));
            if let Some(d) = app2.try_state::<Diag>() {
                let mut f = d.flash.lock().unwrap_or_else(|e| e.into_inner());
                if f.as_ref()
                    .map(|(_, at)| at.elapsed() >= Duration::from_secs(8))
                    .unwrap_or(false)
                {
                    *f = None;
                }
            }
            refresh_tray(&app2);
        });
    }
}

// ---------------------------------------------------------------- 引擎状态

pub fn engine_starting(app: &AppHandle) {
    if let Some(d) = app.try_state::<Diag>() {
        let mut e = d.engine.lock().unwrap_or_else(|e| e.into_inner());
        *e = EngineStatus {
            state: "starting".into(),
            since_epoch: now_epoch(),
            ..Default::default()
        };
    }
    refresh_tray(app);
}

pub fn engine_start_failed(app: &AppHandle, error: &str) {
    if let Some(d) = app.try_state::<Diag>() {
        let mut e = d.engine.lock().unwrap_or_else(|e| e.into_inner());
        *e = EngineStatus {
            state: "failed".into(),
            detail: error.to_string(),
            since_epoch: now_epoch(),
            ..Default::default()
        };
    }
    record(
        app,
        "error",
        "engine_start_failed",
        "引擎启动失败",
        json!({ "error": error }),
    );
}

pub fn engine_ready(app: &AppHandle, port: u16, pid: u32, started_at: u64) {
    if let Some(d) = app.try_state::<Diag>() {
        let mut e = d.engine.lock().unwrap_or_else(|e| e.into_inner());
        *e = EngineStatus {
            state: "ok".into(),
            port,
            pid,
            started_at,
            since_epoch: now_epoch(),
            last_ok_epoch: now_epoch(),
            ..Default::default()
        };
    }
    set_rooms_enabled(app, true);
    record(
        app,
        "info",
        "engine_ready",
        "引擎就绪",
        json!({ "port": port, "pid": pid }),
    );
}

/// 看门狗发现引擎恢复：端口可能变，但 pid/启动时间沿用已有值。
pub fn engine_ok(app: &AppHandle, port: u16) {
    if let Some(d) = app.try_state::<Diag>() {
        let mut e = d.engine.lock().unwrap_or_else(|e| e.into_inner());
        e.state = "ok".into();
        e.port = port;
        e.since_epoch = now_epoch();
        e.last_ok_epoch = now_epoch();
        e.detail.clear();
    }
    set_rooms_enabled(app, true);
}

/// 引擎不可用时把「多房间采集」置灰：点开也只会得到一个连不上的控制台。
fn set_rooms_enabled(app: &AppHandle, enabled: bool) {
    if let Some(d) = app.try_state::<Diag>() {
        let tray = d.tray.lock().unwrap_or_else(|e| e.into_inner());
        if let Some(t) = tray.as_ref() {
            let _ = t.rooms_item.set_enabled(enabled);
        }
    }
}

fn engine_down(app: &AppHandle, state: &str, kind: &str, message: &str, detail: Value) {
    let was_ok = if let Some(d) = app.try_state::<Diag>() {
        let mut guard = d.engine.lock().unwrap_or_else(|e| e.into_inner());
        let was_ok = guard.state == "ok";
        guard.state = state.to_string();
        guard.detail = message.to_string();
        guard.since_epoch = now_epoch();
        guard.failures = guard.failures.saturating_add(1);
        drop(guard);
        d.stalls.lock().unwrap_or_else(|e| e.into_inner()).clear();
        was_ok
    } else {
        false
    };
    record(app, "error", kind, message, detail);
    set_rooms_enabled(app, false);
    if was_ok {
        let app2 = app.clone();
        let _ = app.clone().run_on_main_thread(move || {
            let _ = open_window(&app2);
        });
    }
}

pub fn engine_exited(app: &AppHandle, code: Option<i32>, tail: &str) {
    let msg = match code {
        Some(c) => format!("引擎进程已退出（exit code {c}）"),
        None => "引擎进程已退出".to_string(),
    };
    engine_down(
        app,
        "exited",
        "engine_exited",
        &msg,
        json!({ "exit_code": code, "engine_log_tail": tail }),
    );
}

pub fn engine_unreachable(app: &AppHandle, error: &str) {
    engine_down(
        app,
        "unreachable",
        "engine_unreachable",
        "引擎健康检查连续失败",
        json!({ "error": error }),
    );
}

pub fn engine_recovered(app: &AppHandle, port: u16) {
    if let Some(d) = app.try_state::<Diag>() {
        let mut e = d.engine.lock().unwrap_or_else(|e| e.into_inner());
        e.state = "ok".into();
        e.port = port;
        e.since_epoch = now_epoch();
        e.last_ok_epoch = now_epoch();
        e.detail.clear();
    }
    set_rooms_enabled(app, true);
    record(
        app,
        "info",
        "engine_recovered",
        "引擎恢复响应",
        json!({ "port": port }),
    );
}

// ---------------------------------------------------------------- 看门狗

pub fn start_watchers(
    app: AppHandle,
    port: u16,
    child: Arc<Mutex<Option<Child>>>,
    engine_log: PathBuf,
) {
    spawn_engine_watch(app.clone(), port, child, engine_log);
    spawn_live_watch(app);
}

fn spawn_engine_watch(
    app: AppHandle,
    port: u16,
    child: Arc<Mutex<Option<Child>>>,
    engine_log: PathBuf,
) {
    let _ = std::thread::Builder::new()
        .name("diag-engine-watch".into())
        .spawn(move || {
            let mut failures = 0u32;
            let mut reported = false;
            loop {
                std::thread::sleep(Duration::from_secs(3));
                if is_shutdown(&app) {
                    break;
                }
                // 子进程退出是最硬的信号：不用等健康检查超时。
                let exited = {
                    let mut guard = child.lock().unwrap_or_else(|e| e.into_inner());
                    match guard.as_mut() {
                        Some(c) => match c.try_wait() {
                            Ok(Some(status)) => Some(status.code()),
                            _ => None,
                        },
                        None => None,
                    }
                };
                if let Some(code) = exited {
                    if !is_shutdown(&app) {
                        let tail = engine::tail_file(&engine_log, 4096);
                        engine_exited(&app, code, &tail);
                    }
                    break;
                }
                match http::get(port, "/api/health", Duration::from_secs(3)) {
                    Ok(r) if r.status == 200 => {
                        failures = 0;
                        let state = app
                            .try_state::<Diag>()
                            .map(|d| {
                                d.engine
                                    .lock()
                                    .unwrap_or_else(|e| e.into_inner())
                                    .state
                                    .clone()
                            })
                            .unwrap_or_default();
                        if state != "ok" && state != "starting" {
                            engine_recovered(&app, port);
                        } else if state == "starting" {
                            engine_ok(&app, port);
                        }
                        reported = false;
                    }
                    Ok(r) => {
                        failures += 1;
                        if failures >= 3 {
                            let state = app
                                .try_state::<Diag>()
                                .map(|d| {
                                    d.engine
                                        .lock()
                                        .unwrap_or_else(|e| e.into_inner())
                                        .state
                                        .clone()
                                })
                                .unwrap_or_default();
                            if state == "ok" && !reported {
                                engine_unreachable(&app, &format!("HTTP {}", r.status));
                                reported = true;
                            }
                        }
                    }
                    Err(e) => {
                        failures += 1;
                        if failures >= 3 {
                            let state = app
                                .try_state::<Diag>()
                                .map(|d| {
                                    d.engine
                                        .lock()
                                        .unwrap_or_else(|e| e.into_inner())
                                        .state
                                        .clone()
                                })
                                .unwrap_or_default();
                            if state == "ok" && !reported {
                                engine_unreachable(&app, &e);
                                reported = true;
                            }
                        }
                    }
                }
            }
        });
}

fn poll_ms() -> u64 {
    std::env::var("LIVENOTES_SHELL_POLL_MS")
        .ok()
        .and_then(|v| v.parse::<u64>().ok())
        .map(|v| v.clamp(500, 60_000))
        .unwrap_or(3000)
}

fn spawn_live_watch(app: AppHandle) {
    let _ = std::thread::Builder::new()
        .name("diag-live-watch".into())
        .spawn(move || {
            loop {
                std::thread::sleep(Duration::from_millis(poll_ms()));
                if is_shutdown(&app) {
                    break;
                }
                let port = app
                    .try_state::<Diag>()
                    .map(|d| d.engine.lock().unwrap_or_else(|e| e.into_inner()).port)
                    .unwrap_or(0);
                if port == 0 {
                    continue;
                }
                let resp = match http::get(port, "/api/diagnostics", Duration::from_secs(3)) {
                    Ok(r) if r.status == 200 => r,
                    _ => {
                        // 引擎可能版本较老（没有 /api/diagnostics）：退回
                        // /api/live/list，至少 last_chunk_ago_s 还能看。
                        if let Ok(r) = http::get(port, "/api/live/list", Duration::from_secs(3)) {
                            if r.status == 200 {
                                if let Ok(v) = r.json() {
                                    update_from_live_list(&app, &v);
                                }
                            }
                        }
                        continue;
                    }
                };
                let Ok(v) = resp.json() else {
                    continue;
                };
                if let Some(limit) = v
                    .pointer("/config/stall_warn_s")
                    .and_then(|x| x.as_f64())
                    .filter(|x| *x > 0.0)
                {
                    if let Some(d) = app.try_state::<Diag>() {
                        if !d.threshold_pinned {
                            *d.stall_threshold.lock().unwrap_or_else(|e| e.into_inner()) = limit;
                        }
                    }
                }
                let sessions = v
                    .get("live")
                    .and_then(|x| x.as_array())
                    .cloned()
                    .unwrap_or_default();
                let threshold = app
                    .try_state::<Diag>()
                    .map(|d| *d.stall_threshold.lock().unwrap_or_else(|e| e.into_inner()))
                    .unwrap_or(STALL_DEFAULT_S);
                let mut seen = HashSet::new();
                for item in sessions {
                    let sid = item.get("id").and_then(|x| x.as_str()).unwrap_or("");
                    if sid.is_empty() {
                        continue;
                    }
                    let title = item.get("title").and_then(|x| x.as_str()).unwrap_or("");
                    let idle = item.get("idle_s").and_then(|x| x.as_f64());
                    if let Some(idle) = idle {
                        seen.insert(sid.to_string());
                        update_stall(&app, sid, title, idle, threshold);
                    }
                }
                expire_missing_stalls(&app, &seen);
            }
        });
}

fn update_from_live_list(app: &AppHandle, v: &Value) {
    let busy: HashSet<String> = v
        .get("sessions")
        .and_then(|x| x.as_array())
        .map(|rows| {
            rows.iter()
                .filter(|r| r.get("running").and_then(|x| x.as_bool()).unwrap_or(false))
                .filter_map(|r| r.get("id").and_then(|x| x.as_str()).map(|s| s.to_string()))
                .collect()
        })
        .unwrap_or_default();
    let threshold = app
        .try_state::<Diag>()
        .map(|d| *d.stall_threshold.lock().unwrap_or_else(|e| e.into_inner()))
        .unwrap_or(STALL_DEFAULT_S);
    for row in v
        .get("sessions")
        .and_then(|x| x.as_array())
        .cloned()
        .unwrap_or_default()
    {
        let sid = row.get("id").and_then(|x| x.as_str()).unwrap_or("");
        if sid.is_empty()
            || !row
                .get("running")
                .and_then(|x| x.as_bool())
                .unwrap_or(false)
        {
            continue;
        }
        if let Some(idle) = row.get("last_chunk_ago_s").and_then(|x| x.as_f64()) {
            let title = row.get("title").and_then(|x| x.as_str()).unwrap_or("");
            update_stall(app, sid, title, idle, threshold);
        }
    }
    expire_missing_stalls(app, &busy);
}

fn update_stall(app: &AppHandle, sid: &str, title: &str, idle: f64, threshold: f64) {
    let Some(d) = app.try_state::<Diag>() else {
        return;
    };
    let mut notify = false;
    let mut recovered = false;
    {
        let mut stalls = d.stalls.lock().unwrap_or_else(|e| e.into_inner());
        let entry = stalls.entry(sid.to_string()).or_insert_with(|| StallInfo {
            sid: sid.to_string(),
            title: title.to_string(),
            idle_s: idle,
            notified: false,
            last_seen: Instant::now(),
            missing_polls: 0,
        });
        entry.title = title.to_string();
        entry.idle_s = idle;
        entry.last_seen = Instant::now();
        entry.missing_polls = 0;
        if idle >= threshold && !entry.notified {
            entry.notified = true;
            notify = true;
        } else if entry.notified && idle < (threshold * 0.5).max(5.0) {
            entry.notified = false;
            recovered = true;
        }
    }
    if notify {
        record(
            app,
            "warn",
            "stream_stalled",
            &format!("{title} 断流 {:.0}s", idle),
            json!({
                "session_id": sid,
                "title": title,
                "idle_s": idle,
                "threshold_s": threshold,
            }),
        );
    }
    if recovered {
        record(
            app,
            "info",
            "stream_recovered",
            &format!("{title} 音频块恢复"),
            json!({ "session_id": sid, "title": title, "idle_s": idle }),
        );
    }
    refresh_tray(app);
}

fn expire_missing_stalls(app: &AppHandle, seen: &HashSet<String>) {
    let Some(d) = app.try_state::<Diag>() else {
        return;
    };
    let mut ended = Vec::new();
    {
        let mut stalls = d.stalls.lock().unwrap_or_else(|e| e.into_inner());
        let mut gone = Vec::new();
        for (sid, entry) in stalls.iter_mut() {
            if seen.contains(sid) {
                continue;
            }
            entry.missing_polls += 1;
            if entry.missing_polls >= 2 {
                gone.push(sid.clone());
                if entry.notified {
                    ended.push(entry.title.clone());
                }
            }
        }
        for sid in gone {
            stalls.remove(&sid);
        }
    }
    for title in ended {
        record(
            app,
            "info",
            "stream_ended",
            &format!("{title} 的场次已结束"),
            json!({ "title": title }),
        );
    }
    refresh_tray(app);
}

// ---------------------------------------------------------------- 房间视图崩溃

/// 房间子视图进程失败。返回是否应当让 WebView2 自动 Reload。
pub fn room_failed(
    app: &AppHandle,
    room: &str,
    kind: &str,
    detail_live: &str,
    can_reload: bool,
) -> bool {
    let epoch = now_epoch();
    let mut reload = false;
    if let Some(d) = app.try_state::<Diag>() {
        let mut crashes = d.crashes.lock().unwrap_or_else(|e| e.into_inner());
        let recent = crashes
            .iter()
            .filter(|c| c.room == room && epoch.saturating_sub(c.epoch) <= RECENT_CRASH_S)
            .count();
        // 同一房间 10 分钟内最多自动重载 3 次；连续崩说明重载救不了，
        // 让用户看到“需要重开这一路”，而不是无限循环。
        reload = can_reload && recent < 3;
        crashes.push_back(CrashInfo {
            room: room.to_string(),
            kind: kind.to_string(),
            detail: detail_live.to_string(),
            epoch,
        });
        while crashes.len() > 60 {
            crashes.pop_front();
        }
    }
    let message = if reload {
        format!("房间 {room} 的视图进程崩溃，已自动重载")
    } else {
        format!("房间 {room} 的视图进程崩溃，需要重新打开这一路")
    };
    record(
        app,
        "error",
        "room_process_failed",
        &message,
        json!({
            "room": room,
            "kind": kind,
            "detail": detail_live,
            "auto_reload": reload,
        }),
    );
    reload
}

// ---------------------------------------------------------------- 自动导出

fn maybe_auto_export(app: &AppHandle, level: &str) {
    if !matches!(level, "warn" | "error") {
        return;
    }
    let enabled = std::env::var("LIVENOTES_DIAG_AUTO_EXPORT")
        .map(|v| matches!(v.trim().to_ascii_lowercase().as_str(), "1" | "true" | "yes"))
        .unwrap_or(false);
    if !enabled {
        return;
    }
    let Some(d) = app.try_state::<Diag>() else {
        return;
    };
    if d.auto_exports.load(Ordering::SeqCst) >= 3 {
        return;
    }
    {
        let mut last = d.last_auto_export.lock().unwrap_or_else(|e| e.into_inner());
        if last
            .as_ref()
            .map(|t| t.elapsed() < Duration::from_secs(5))
            .unwrap_or(false)
        {
            return;
        }
        *last = Some(Instant::now());
    }
    d.auto_exports.fetch_add(1, Ordering::SeqCst);
    let app = app.clone();
    std::thread::spawn(move || {
        std::thread::sleep(Duration::from_millis(800));
        match export(&app) {
            Ok(path) => {
                engine::append_log(
                    &engine::app_data_root()
                        .unwrap_or_default()
                        .join("logs")
                        .join("shell.log"),
                    &format!("diag auto export: {}", path.display()),
                );
            }
            Err(e) => {
                engine::append_log(
                    &engine::app_data_root()
                        .unwrap_or_default()
                        .join("logs")
                        .join("shell.log"),
                    &format!("diag auto export failed: {e}"),
                );
            }
        }
    });
}

// ---------------------------------------------------------------- 导出诊断包

pub fn desktop_dir() -> Option<PathBuf> {
    let home = std::env::var_os("USERPROFILE")?;
    let home = PathBuf::from(home);
    let direct = home.join("Desktop");
    if direct.is_dir() {
        return Some(direct);
    }
    let onedrive = home.join("OneDrive").join("Desktop");
    if onedrive.is_dir() {
        return Some(onedrive);
    }
    None
}

pub fn export(app: &AppHandle) -> Result<PathBuf, String> {
    let Some(d) = app.try_state::<Diag>() else {
        return Err("诊断状态尚未初始化".into());
    };
    let _guard = d.export_lock.lock().unwrap_or_else(|e| e.into_inner());
    let root = engine::app_data_root()?;
    let out_dir = std::env::var_os("LIVENOTES_DIAG_DIR")
        .map(PathBuf::from)
        .or_else(desktop_dir)
        .unwrap_or_else(|| root.clone());
    fs::create_dir_all(&out_dir).map_err(|e| format!("创建导出目录 {}: {e}", out_dir.display()))?;
    let epoch = now_epoch();
    let path = out_dir.join(format!("LiveNotes-diagnostics-{}.zip", compact_utc(epoch)));

    let engine_status = d.engine.lock().unwrap_or_else(|e| e.into_inner()).clone();
    let threshold = *d.stall_threshold.lock().unwrap_or_else(|e| e.into_inner());
    let events: Vec<DiagEvent> = d
        .events
        .lock()
        .unwrap_or_else(|e| e.into_inner())
        .iter()
        .cloned()
        .collect();
    let stalls: Vec<Value> = d
        .stalls
        .lock()
        .unwrap_or_else(|e| e.into_inner())
        .values()
        .map(|s| {
            json!({
                "session_id": s.sid.as_str(),
                "title": s.title.as_str(),
                "idle_s": s.idle_s,
                "notified": s.notified,
            })
        })
        .collect();
    let crashes: Vec<Value> = {
        let crashes = d.crashes.lock().unwrap_or_else(|e| e.into_inner());
        crashes
            .iter()
            .map(|c| {
                json!({
                    "ts": iso_utc(c.epoch),
                    "epoch": c.epoch,
                    "room": c.room.as_str(),
                    "kind": c.kind.as_str(),
                    "detail": c.detail.as_str(),
                })
            })
            .collect()
    };

    let result = write_bundle(
        app,
        &path,
        &root,
        &engine_status,
        threshold,
        &events,
        &stalls,
        &crashes,
    );
    match result {
        Ok(()) => {
            *d.last_export.lock().unwrap_or_else(|e| e.into_inner()) = Some((path.clone(), epoch));
            record(
                app,
                "info",
                "diagnostics_exported",
                &format!("诊断包已导出：{}", path.display()),
                json!({ "path": path.to_string_lossy() }),
            );
            flash_status(app, "诊断包已导出".to_string());
            Ok(path)
        }
        Err(e) => {
            let _ = fs::remove_file(&path);
            record(
                app,
                "error",
                "diagnostics_export_failed",
                "诊断包导出失败",
                json!({ "error": e, "path": path.to_string_lossy() }),
            );
            Err(e)
        }
    }
}

#[allow(clippy::too_many_arguments)]
fn write_bundle(
    app: &AppHandle,
    path: &Path,
    root: &Path,
    engine_status: &EngineStatus,
    threshold: f64,
    events: &[DiagEvent],
    stalls: &[Value],
    crashes: &[Value],
) -> Result<(), String> {
    let file = File::create(path).map_err(|e| format!("创建 {}: {e}", path.display()))?;
    let mut zip = ZipWriter::new(file);
    let opts = SimpleFileOptions::default()
        .compression_method(CompressionMethod::Deflated)
        .unix_permissions(0o644);

    zip_write_text(
        &mut zip,
        opts,
        "README.txt",
        &format!(
            "LiveNotes 诊断包\n\
             ================\n\
             生成时间（UTC）：{}\n\
             壳版本：{}\n\n\
             包含：\n\
             - summary.json            壳与引擎状态、失败事件、路径、环境变量（密钥已打码）\n\
             - diag/events.json        壳记录的最近失败事件（与 logs/diag.jsonl 一致）\n\
             - snapshots/*.json        导出瞬间引擎的 /api/health、/api/diagnostics 快照\n\
             - logs/shell.log          壳日志（超过 8MB 只保留尾部）\n\
             - logs/engine.log         引擎 stdout/stderr（超过 8MB 只保留尾部）\n\
             - logs/app-*.jsonl        引擎结构化日志，最近 3 个文件\n\
             - logs/diag.jsonl         壳的机器可读诊断事件\n\
             - crash-dumps/            WebView2 崩溃转储（存在时最多 5 个）\n\n\
             不包含：settings.json、LLM Key、录音、字幕正文（含 /api/live/list 的\n\
             last_line）、/api/rooms 的原始 URL；直播间 URL 只保留 host+path。\n\n\
             排查顺序：先看 summary.json 的 events，再看 logs/engine.log 尾部；\n\
             房间视图崩溃看 crash-dumps；断流看 snapshots/diagnostics.json 的 live。\n",
            iso_utc(now_epoch()),
            env!("CARGO_PKG_VERSION"),
        ),
    )?;

    let room_stats = room_stats_json(app);
    let dumps = find_dumps(&root.join("webview2"));
    let envs = safe_env();
    let summary = json!({
        "created_at": iso_utc(now_epoch()),
        "epoch": now_epoch(),
        "shell": {
            "version": env!("CARGO_PKG_VERSION"),
            "exe": std::env::current_exe().map(|p| p.to_string_lossy().into_owned()).unwrap_or_default(),
            "pid": std::process::id(),
            "cwd": std::env::current_dir().map(|p| p.to_string_lossy().into_owned()).unwrap_or_default(),
            "os": std::env::consts::OS,
            "arch": std::env::consts::ARCH,
        },
        "paths": {
            "home": root.to_string_lossy(),
            "data": root.join("data").to_string_lossy(),
            "logs": root.join("logs").to_string_lossy(),
            "webview2": root.join("webview2").to_string_lossy(),
        },
        "engine": {
            "state": engine_status.state.as_str(),
            "port": engine_status.port,
            "detail": engine_status.detail.as_str(),
            "since": iso_utc(engine_status.since_epoch),
            "last_ok": if engine_status.last_ok_epoch > 0 { Some(iso_utc(engine_status.last_ok_epoch)) } else { None },
        },
        "stall_threshold_s": threshold,
        "stalls": stalls,
        "room_crashes": crashes,
        "room_stats": room_stats,
        "env": envs,
        "crash_dumps": dumps.iter().map(|p| p.to_string_lossy()).collect::<Vec<_>>(),
        "privacy": "不含 settings.json / LLM Key / 录音 / 字幕正文；直播 URL 只保留 host+path。",
    });
    zip_write_json(&mut zip, opts, "summary.json", &summary)?;
    zip_write_json(&mut zip, opts, "diag/events.json", &json!(events))?;

    // 只导出整理过的快照：/api/live/list 带 last_line（字幕片段），
    // /api/rooms 带原始直播间 URL（query 可能有 token），都不进诊断包。
    for (name, api) in [
        ("snapshots/health.json", "/api/health"),
        ("snapshots/diagnostics.json", "/api/diagnostics"),
    ] {
        let value = fetch_json(engine_status.port, api);
        zip_write_json(&mut zip, opts, name, &value)?;
    }

    let logs = root.join("logs");
    add_file_limited(
        &mut zip,
        opts,
        &logs.join("shell.log"),
        "logs/shell.log",
        TAIL_BYTES,
    )?;
    add_file_limited(
        &mut zip,
        opts,
        &logs.join("engine.log"),
        "logs/engine.log",
        TAIL_BYTES,
    )?;
    add_file_limited(
        &mut zip,
        opts,
        &logs.join("diag.jsonl"),
        "logs/diag.jsonl",
        TAIL_BYTES,
    )?;
    let mut app_logs = recent_app_logs(&root.join("data").join("logs"), 3);
    app_logs.sort();
    for src in app_logs {
        if let Some(name) = src.file_name().and_then(|s| s.to_str()) {
            add_file_limited(&mut zip, opts, &src, &format!("logs/{name}"), TAIL_BYTES)?;
        }
    }
    for src in dumps.iter().take(5) {
        if let Some(name) = src.file_name().and_then(|s| s.to_str()) {
            add_file_limited(
                &mut zip,
                opts,
                src,
                &format!("crash-dumps/{name}"),
                8 * 1024 * 1024,
            )?;
        }
    }
    zip.finish()
        .map(|_| ())
        .map_err(|e| format!("写入 zip: {e}"))
}

fn zip_write_text<W: Write + Seek>(
    zip: &mut ZipWriter<W>,
    opts: SimpleFileOptions,
    name: &str,
    text: &str,
) -> Result<(), String> {
    zip.start_file(name, opts)
        .map_err(|e| format!("zip {name}: {e}"))?;
    zip.write_all(text.as_bytes())
        .map_err(|e| format!("zip {name}: {e}"))
}

fn zip_write_json<W: Write + Seek>(
    zip: &mut ZipWriter<W>,
    opts: SimpleFileOptions,
    name: &str,
    value: &Value,
) -> Result<(), String> {
    let body = serde_json::to_vec_pretty(value).map_err(|e| e.to_string())?;
    zip.start_file(name, opts)
        .map_err(|e| format!("zip {name}: {e}"))?;
    zip.write_all(&body).map_err(|e| format!("zip {name}: {e}"))
}

fn add_file_limited<W: Write + Seek>(
    zip: &mut ZipWriter<W>,
    opts: SimpleFileOptions,
    src: &Path,
    name: &str,
    max: u64,
) -> Result<(), String> {
    let mut f = match File::open(src) {
        Ok(f) => f,
        Err(_) => return Ok(()), // 文件不存在不算失败：日志可能还没生成
    };
    let len = f.metadata().map(|m| m.len()).unwrap_or(0);
    let start = len.saturating_sub(max);
    zip.start_file(name, opts)
        .map_err(|e| format!("zip {name}: {e}"))?;
    if start > 0 {
        let note = format!(
            "…[truncated] 原始 {} 字节，以下为最后 {} 字节 …\n",
            len,
            len - start
        );
        zip.write_all(note.as_bytes())
            .map_err(|e| format!("zip {name}: {e}"))?;
        f.seek(SeekFrom::Start(start))
            .map_err(|e| format!("seek {src:?}: {e}"))?;
    }
    let mut buf = [0u8; 64 * 1024];
    loop {
        let n = f.read(&mut buf).map_err(|e| format!("read {src:?}: {e}"))?;
        if n == 0 {
            break;
        }
        zip.write_all(&buf[..n])
            .map_err(|e| format!("zip {name}: {e}"))?;
    }
    Ok(())
}

fn fetch_json(port: u16, path: &str) -> Value {
    if port == 0 {
        return json!({ "error": "engine port unknown" });
    }
    match http::get(port, path, Duration::from_secs(3)) {
        Ok(r) if r.status == 200 => r
            .json()
            .unwrap_or_else(|_| json!({ "http": r.status, "body": r.text() })),
        Ok(r) => json!({ "http": r.status, "body": r.text() }),
        Err(e) => json!({ "error": e }),
    }
}

fn room_stats_json(app: &AppHandle) -> Value {
    let Some(state) = app.try_state::<crate::rooms::RoomState>() else {
        return json!([]);
    };
    let now = Instant::now();
    let rows: Vec<Value> = state
        .stats
        .lock()
        .unwrap_or_else(|e| e.into_inner())
        .iter()
        .map(|(room, s)| {
            json!({
                "room": room,
                "session_id": s.sid.as_str(),
                "chunks": s.chunks,
                "bytes": s.bytes,
                "drops": s.drops,
                "last_ms_ago": s.last.map(|t| now.duration_since(t).as_millis() as u64),
            })
        })
        .collect();
    json!(rows)
}

fn recent_app_logs(dir: &Path, keep: usize) -> Vec<PathBuf> {
    let Ok(rd) = fs::read_dir(dir) else {
        return Vec::new();
    };
    let mut files: Vec<(SystemTime, PathBuf)> = rd
        .flatten()
        .filter_map(|e| {
            let p = e.path();
            let name = p.file_name()?.to_string_lossy();
            if !(name.starts_with("app-") && name.ends_with(".jsonl")) {
                return None;
            }
            let mtime = e
                .metadata()
                .and_then(|m| m.modified())
                .unwrap_or(UNIX_EPOCH);
            Some((mtime, p))
        })
        .collect();
    files.sort_by(|a, b| b.0.cmp(&a.0));
    files.into_iter().take(keep).map(|(_, p)| p).collect()
}

fn find_dumps(dir: &Path) -> Vec<PathBuf> {
    fn walk(dir: &Path, depth: usize, out: &mut Vec<(SystemTime, PathBuf)>) {
        if depth > 6 {
            return;
        }
        let Ok(rd) = fs::read_dir(dir) else {
            return;
        };
        for e in rd.flatten() {
            let p = e.path();
            if p.is_dir() {
                walk(&p, depth + 1, out);
            } else if p
                .extension()
                .and_then(|x| x.to_str())
                .map(|x| x.eq_ignore_ascii_case("dmp"))
                .unwrap_or(false)
            {
                let mtime = e
                    .metadata()
                    .and_then(|m| m.modified())
                    .unwrap_or(UNIX_EPOCH);
                out.push((mtime, p));
            }
        }
    }
    let mut out = Vec::new();
    walk(dir, 0, &mut out);
    out.sort_by(|a, b| b.0.cmp(&a.0));
    out.into_iter().take(5).map(|(_, p)| p).collect()
}

fn safe_env() -> Map<String, Value> {
    let mut out = Map::new();
    for (k, v) in std::env::vars() {
        if !k.starts_with("LIVENOTES_") {
            continue;
        }
        let lower = k.to_ascii_lowercase();
        let sensitive = ["key", "token", "secret", "password", "cookie"]
            .iter()
            .any(|s| lower.contains(s));
        out.insert(
            k,
            if sensitive {
                json!("<redacted>")
            } else {
                json!(v)
            },
        );
    }
    out
}

// ---------------------------------------------------------------- 窗口与命令

pub fn open_window(app: &AppHandle) -> Option<tauri::WebviewWindow> {
    if let Some(w) = app.get_webview_window("diag") {
        let _ = w.show();
        let _ = w.set_focus();
        return Some(w);
    }
    let builder =
        WebviewWindowBuilder::new(app, "diag", WebviewUrl::App("diagnostics.html".into()))
            .title("LiveNotes · 诊断")
            .inner_size(900.0, 680.0)
            .min_inner_size(640.0, 480.0);
    let builder = match engine::webview_dir("diag") {
        Ok(dir) => builder.data_directory(dir),
        Err(_) => builder,
    };
    let win = builder.build().ok()?;
    #[cfg(windows)]
    crate::tame_webview(&win);
    Some(win)
}

pub fn open_logs(app: &AppHandle) {
    if let Ok(root) = engine::app_data_root() {
        let logs = root.join("logs");
        let _ = fs::create_dir_all(&logs);
        let _ = app
            .opener()
            .open_path(logs.to_string_lossy().into_owned(), None::<&str>);
    }
}

pub fn reveal_file(app: &AppHandle, path: &Path) {
    let _ = app.opener().reveal_item_in_dir(path);
}

fn snapshot_json(_app: &AppHandle, d: &Diag) -> Value {
    let view = compute_status(d);
    let engine = d.engine.lock().unwrap_or_else(|e| e.into_inner()).clone();
    let events: Vec<DiagEvent> = d
        .events
        .lock()
        .unwrap_or_else(|e| e.into_inner())
        .iter()
        .rev()
        .take(80)
        .cloned()
        .collect();
    let stalls: Vec<Value> = d
        .stalls
        .lock()
        .unwrap_or_else(|e| e.into_inner())
        .values()
        .map(|s| {
            json!({
                "session_id": s.sid.as_str(),
                "title": s.title.as_str(),
                "idle_s": s.idle_s,
                "notified": s.notified,
            })
        })
        .collect();
    let crashes: Vec<Value> = d
        .crashes
        .lock()
        .unwrap_or_else(|e| e.into_inner())
        .iter()
        .rev()
        .take(20)
        .map(|c| {
            json!({
                "ts": iso_utc(c.epoch),
                "room": c.room.as_str(),
                "kind": c.kind.as_str(),
                "detail": c.detail.as_str(),
            })
        })
        .collect();
    let last_export = d
        .last_export
        .lock()
        .unwrap_or_else(|e| e.into_inner())
        .clone();
    let root = engine::app_data_root().unwrap_or_default();
    json!({
        "status": { "text": view.text, "tooltip": view.tooltip, "warn": view.warn },
        "engine": {
            "state": engine.state,
            "port": engine.port,
            "pid": engine.pid,
            "started_at": if engine.started_at > 0 { Some(iso_utc(engine.started_at)) } else { None },
            "detail": engine.detail,
            "since": iso_utc(engine.since_epoch),
            "last_ok": if engine.last_ok_epoch > 0 { Some(iso_utc(engine.last_ok_epoch)) } else { None },
        },
        "stall_threshold_s": *d.stall_threshold.lock().unwrap_or_else(|e| e.into_inner()),
        "stalls": stalls,
        "crashes": crashes,
        "events": events,
        "paths": {
            "home": root.to_string_lossy(),
            "logs": root.join("logs").to_string_lossy(),
            "data": root.join("data").to_string_lossy(),
        },
        "last_export": last_export.map(|(p, e)| json!({
            "path": p.to_string_lossy(),
            "ts": iso_utc(e),
        })),
        "shell_pid": std::process::id(),
    })
}

#[tauri::command]
pub fn diag_snapshot(app: AppHandle, state: State<'_, Diag>) -> Value {
    snapshot_json(&app, &state)
}

#[tauri::command]
pub fn diag_export(app: AppHandle, state: State<'_, Diag>) -> Result<String, String> {
    let _ = &state;
    let path = export(&app)?;
    reveal_file(&app, &path);
    Ok(path.to_string_lossy().into_owned())
}

#[tauri::command]
pub fn diag_open_logs(app: AppHandle) {
    open_logs(&app);
}

#[tauri::command]
/// 和 `live_open_view` 一样必须是 async：同步命令在事件循环线程里建 WebView2
/// 窗口会把消息泵堵死，诊断页点一下整壳就卡住。
pub async fn diag_open_window(app: AppHandle) {
    let _ = open_window(&app);
}

/// 清理 engine.json 记录的残留引擎。当前正在用的引擎不允许从诊断页杀。
#[tauri::command]
pub fn diag_engine_kill(app: AppHandle) -> Result<String, String> {
    let root = engine::app_data_root()?;
    let path = root.join("data").join("runtime").join("engine.json");
    let raw = std::fs::read_to_string(&path).map_err(|e| format!("没有 engine.json：{e}"))?;
    let v: Value =
        serde_json::from_str(&raw).map_err(|e| format!("engine.json 解析失败：{e}"))?;
    let pid = v.get("pid").and_then(|x| x.as_u64()).unwrap_or(0) as u32;
    if pid == 0 {
        return Err("engine.json 里没有 pid".into());
    }
    if let Some(state) = app.try_state::<crate::ShellState>() {
        if state.engine.pid == pid {
            return Err("这是当前正在使用的引擎，请用托盘「退出 LiveNotes」".into());
        }
    }
    if !engine::is_engine_process(pid) {
        return Err(format!("pid={pid} 不是引擎进程，未执行"));
    }
    engine::terminate_pid(pid);
    let _ = std::fs::remove_file(&path);
    Ok(format!("已结束残留引擎 pid={pid}"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn utc_formatting_is_stable() {
        assert_eq!(iso_utc(0), "1970-01-01T00:00:00Z");
        assert_eq!(iso_utc(1_700_000_000), "2023-11-14T22:13:20Z");
        assert_eq!(compact_utc(1_700_000_000), "20231114-221320Z");
    }

    #[test]
    fn tray_status_puts_engine_before_streams() {
        let view = status_view("ok", 1, 0, None);
        assert_eq!(view.text, "⚠ 1 路断流 · 打开诊断");
        assert!(view.warn);

        // 引擎整体挂了比“某一路断流”更严重，必须先显示引擎。
        let view = status_view("exited", 1, 0, None);
        assert!(view.text.contains("引擎已退出"), "{}", view.text);
        assert!(view.tooltip.contains("右键托盘导出诊断"));
    }

    #[test]
    fn warning_icon_keeps_size_and_marks_corner() {
        let base = Image::new_owned(vec![0u8; 64 * 64 * 4], 64, 64);
        let icon = warning_icon(&base).expect("badge");
        assert_eq!((icon.width(), icon.height()), (64, 64));
        let px = icon.rgba();
        let painted = px.chunks_exact(4).filter(|p| p[3] > 0).count();
        assert!(painted > 100, "角标没有画上：{painted} 个不透明像素");
    }
}
