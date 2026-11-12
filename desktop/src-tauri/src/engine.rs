//! Python 引擎进程的启动、健康检查与回收。
//!
//! 开发态直接 `python run.py`；打包态用 `resources/engine/engine.exe`
//! （PyInstaller onedir）。两种形态都只靠环境变量通信：
//! LIVENOTES_PORT / LIVENOTES_DATA_DIR / LIVENOTES_NO_BROWSER。

use std::fs::{File, OpenOptions};
use std::io::{Read, Seek, SeekFrom, Write};
use std::net::TcpListener;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, LazyLock, Mutex};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use tauri::{AppHandle, Manager};

use crate::http;

#[cfg(windows)]
use std::os::windows::io::AsRawHandle;
#[cfg(windows)]
use windows::Win32::Foundation::{CloseHandle, HANDLE};
#[cfg(windows)]
use windows::Win32::System::JobObjects::{
    AssignProcessToJobObject, CreateJobObjectW, JobObjectExtendedLimitInformation,
    SetInformationJobObject, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
};
#[cfg(windows)]
use windows::Win32::System::Threading::{
    OpenProcess, PROCESS_SET_QUOTA, PROCESS_TERMINATE,
};

#[cfg(windows)]
use std::os::windows::process::CommandExt;

#[cfg(windows)]
const CREATE_NO_WINDOW: u32 = 0x0800_0000;

/// Windows Job Object：壳进程无论怎么死（崩溃、任务管理器、关终端），
/// 只要句柄关闭，系统就会把引擎和它拉起的 helper 整棵树杀掉。
/// 这是不依赖 Tauri 走到 Exit 事件的兜底。
#[cfg(windows)]
struct JobHandle(HANDLE);

#[cfg(windows)]
unsafe impl Send for JobHandle {}
#[cfg(windows)]
unsafe impl Sync for JobHandle {}

#[cfg(windows)]
impl Drop for JobHandle {
    fn drop(&mut self) {
        unsafe {
            let _ = CloseHandle(self.0);
        }
    }
}

pub struct Engine {
    pub port: u16,
    pub pid: u32,
    pub started_at: u64,
    pub data_dir: PathBuf,
    pub log_path: PathBuf,
    /// true = 复用了上一次运行留下的健康引擎，没有子进程句柄。
    adopted: bool,
    /// 防止 Exit 事件和 Drop 各回收一次：第二次 taskkill 时 PID 可能已被
    /// 系统复用，误杀别的进程。
    stopped: AtomicBool,
    child: Arc<Mutex<Option<Child>>>,
    #[cfg(windows)]
    job: Mutex<Option<JobHandle>>,
}

/// 建目录。某些环境（沙箱、杀软、重定向）对已存在目录也会返回
/// ERROR_ALREADY_EXISTS；确认它确实是目录就继续，别把正常启动挡在门口。
fn ensure_dir(path: &Path) -> Result<(), String> {
    match std::fs::create_dir_all(path) {
        Ok(()) => Ok(()),
        Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists && path.is_dir() => Ok(()),
        Err(e) => Err(format!("create {}: {e}", path.display())),
    }
}

impl Engine {
    /// 优先复用上一次运行留下的健康引擎；没有就新起一个。
    ///
    /// 复用很关键：壳崩溃/被强杀后引擎可能还活着，再起一个就会出现两套引擎
    /// 共用同一个库、飞书重复回复。engine.json 里记了 pid/port，先探活再决定。
    pub fn acquire(app: &AppHandle) -> Result<Engine, String> {
        let base = app_data_root()?;
        let data_dir = base.join("data");
        let log_dir = base.join("logs");
        ensure_dir(&data_dir)?;
        ensure_dir(&log_dir)?;
        let log_path = log_dir.join("engine.log");
        if let Some(engine) = try_adopt(&data_dir, &log_path) {
            return Ok(engine);
        }
        Engine::spawn(app, data_dir, log_path)
    }

    fn spawn(app: &AppHandle, data_dir: PathBuf, log_path: PathBuf) -> Result<Engine, String> {
        let port = pick_port()?;
        // 引擎的临时文件也放进数据目录：打包态不污染用户 TEMP；
        // 受限令牌/沙箱下 `%TEMP%` 可能不可写，SQLite 建 journal 会直接失败。
        let tmp_dir = data_dir.join("tmp");
        ensure_dir(&tmp_dir)?;

        let (program, args, cwd) = engine_command(app)?;
        append_log(
            &log_path,
            &format!(
                "starting engine: {program:?} {args:?} cwd={} port={port} data={}",
                cwd.display(),
                data_dir.display()
            ),
        );

        let stdout = OpenOptions::new()
            .create(true)
            .append(true)
            .open(&log_path)
            .map_err(|e| format!("open engine log: {e}"))?;
        let stderr = stdout
            .try_clone()
            .map_err(|e| format!("clone engine log: {e}"))?;

        let mut cmd = Command::new(&program);
        cmd.args(&args)
            .current_dir(&cwd)
            .env("LIVENOTES_PORT", port.to_string())
            .env("LIVENOTES_HOST", "127.0.0.1")
            .env("LIVENOTES_NO_BROWSER", "1")
            // 桌面端没有 tools/start.bat，让引擎自己拉起飞书机器人和采集代理：
            // 飞书里发的直播间链接会变成房间，由采集代理开 Chrome 采音。
            .env("LIVENOTES_HELPERS", "1")
            .env("LIVENOTES_DATA_DIR", &data_dir)
            // 父进程看门狗：壳被硬终止时引擎自己收尾，不依赖 Tauri 的 Exit 事件。
            .env("LIVENOTES_PARENT_PID", std::process::id().to_string())
            .env("TMP", &tmp_dir)
            .env("TEMP", &tmp_dir)
            .env("PYTHONUNBUFFERED", "1")
            .env("PYTHONIOENCODING", "utf-8")
            .stdin(Stdio::null())
            .stdout(Stdio::from(stdout))
            .stderr(Stdio::from(stderr));
        #[cfg(windows)]
        cmd.creation_flags(CREATE_NO_WINDOW);

        // 打包态：模型和 ffmpeg 随 resources 走；开发态不设置，引擎会用仓库
        // 根的 models/ 和 PATH 里的 ffmpeg。
        if let Some(res) = resource_base(app) {
            let models = res.join("models");
            if models.is_dir() {
                cmd.env("LIVENOTES_MODELS_DIR", &models);
            }
            let ffmpeg_dir = res.join("ffmpeg");
            if ffmpeg_dir.is_dir() {
                let old_path = std::env::var("PATH").unwrap_or_default();
                cmd.env("PATH", format!("{};{}", ffmpeg_dir.display(), old_path));
                let ffmpeg_exe = ffmpeg_dir.join("ffmpeg.exe");
                if ffmpeg_exe.is_file() {
                    cmd.env("LIVENOTES_FFMPEG", &ffmpeg_exe);
                }
            }
        }

        // Job 必须在 spawn 前建好：壳一旦消失，句柄关闭，系统回收整棵树。
        #[cfg(windows)]
        let job = create_kill_job();

        let mut child = cmd
            .spawn()
            .map_err(|e| format!("spawn engine ({program}): {e}"))?;
        let pid = child.id();
        #[cfg(windows)]
        if let Some(j) = &job {
            let h = HANDLE(child.as_raw_handle());
            if let Err(e) = unsafe { AssignProcessToJobObject(j.0, h) } {
                append_log(&log_path, &format!("assign engine to job failed: {e}"));
            }
        }

        let deadline = Instant::now() + Duration::from_secs(75);
        loop {
            if let Ok(Some(status)) = child.try_wait() {
                // 退出码 3 = 同一数据目录已有引擎（runtime 锁被占）。
                // 等对方健康后直接复用，避免「启动失败」。
                if status.code() == Some(3) {
                    for _ in 0..10 {
                        if let Some(engine) = try_adopt(&data_dir, &log_path) {
                            return Ok(engine);
                        }
                        std::thread::sleep(Duration::from_millis(200));
                    }
                }
                let tail = tail_file(&log_path, 4096);
                return Err(format!(
                    "engine exited before health check: {status}\n--- engine.log tail ---\n{tail}"
                ));
            }
            match http::get(port, "/api/health", Duration::from_millis(800)) {
                Ok(r) if r.status == 200 => break,
                _ => {}
            }
            if Instant::now() >= deadline {
                let _ = terminate_tree(child.id(), true);
                let _ = child.wait();
                let tail = tail_file(&log_path, 4096);
                return Err(format!(
                    "engine health check timed out after 75s\n--- engine.log tail ---\n{tail}"
                ));
            }
            std::thread::sleep(Duration::from_millis(250));
        }

        append_log(&log_path, &format!("engine ready on 127.0.0.1:{port}"));
        Ok(Engine {
            port,
            pid,
            started_at: now_epoch(),
            data_dir,
            log_path,
            adopted: false,
            stopped: AtomicBool::new(false),
            child: Arc::new(Mutex::new(Some(child))),
            #[cfg(windows)]
            job: Mutex::new(job),
        })
    }

    pub fn base_url(&self) -> String {
        format!("http://127.0.0.1:{}/", self.port)
    }

    /// 诊断看门狗要直接看子进程有没有退出；和 `stop()` 共用同一把锁，
    /// 退出流程里不会和回收逻辑抢 Child。
    pub fn child_handle(&self) -> Arc<Mutex<Option<Child>>> {
        self.child.clone()
    }

    pub fn log_path(&self) -> &Path {
        &self.log_path
    }

    /// 先礼后兵：taskkill /T（不带 /F）给进程树一次退出机会，2 秒后强杀。
    /// 即使 Tauri 没有走到 Exit 事件，Drop 也会兜底回收。
    /// Python 没有注册 Ctrl-Break 处理器，正常路径通常走强杀，但这样至少
    /// 不会把引擎将来加的优雅退出路径堵死。
    pub fn stop(&self) {
        if self.stopped.swap(true, Ordering::SeqCst) {
            return;
        }
        let mut guard = match self.child.lock() {
            Ok(g) => g,
            Err(p) => p.into_inner(),
        };
        if let Some(mut child) = guard.take() {
            let pid = child.id();
            append_log(&self.log_path, &format!("stopping engine pid={pid}"));
            let _ = terminate_tree(pid, false);
            let deadline = Instant::now() + Duration::from_secs(2);
            let mut exited = false;
            while Instant::now() < deadline {
                if let Ok(Some(_)) = child.try_wait() {
                    append_log(&self.log_path, "engine exited gracefully");
                    exited = true;
                    break;
                }
                std::thread::sleep(Duration::from_millis(100));
            }
            if !exited {
                let _ = child.kill();
                let _ = child.wait();
                let _ = terminate_tree(pid, true);
                append_log(&self.log_path, "engine killed");
            }
        } else if self.adopted {
            // 复用的引擎没有 Child 句柄，按 engine.json 里的 pid 回收。
            append_log(
                &self.log_path,
                &format!("stopping adopted engine pid={}", self.pid),
            );
            let _ = terminate_tree(self.pid, true);
        }
        // 关掉 Job：即使前面 taskkill 漏了刚拉起的 helper，这里也会被系统回收。
        #[cfg(windows)]
        {
            if let Ok(mut j) = self.job.lock() {
                let _ = j.take();
            }
        }
        self.cleanup_runtime();
    }

    fn cleanup_runtime(&self) {
        let path = self.data_dir.join("runtime").join("engine.json");
        let Ok(raw) = std::fs::read_to_string(&path) else {
            return;
        };
        let Ok(v) = serde_json::from_str::<serde_json::Value>(&raw) else {
            return;
        };
        if v.get("pid").and_then(|x| x.as_u64()) == Some(self.pid as u64) {
            let _ = std::fs::remove_file(path);
        }
    }
}

impl Drop for Engine {
    fn drop(&mut self) {
        self.stop();
    }
}

fn now_epoch() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0)
}

fn read_runtime(data_dir: &Path) -> Option<serde_json::Value> {
    let path = data_dir.join("runtime").join("engine.json");
    let raw = std::fs::read_to_string(path).ok()?;
    serde_json::from_str(&raw).ok()
}

/// 读 engine.json，确认端口上的引擎就是记录里的那个进程，才复用。
fn try_adopt(data_dir: &Path, log_path: &Path) -> Option<Engine> {
    let v = read_runtime(data_dir)?;
    let pid = v.get("pid")?.as_u64()? as u32;
    let port = v.get("port")?.as_u64()? as u16;
    if pid == 0 || port == 0 {
        return None;
    }
    let resp = http::get(port, "/api/health", Duration::from_millis(1200)).ok()?;
    if resp.status != 200 {
        return None;
    }
    let health = resp.json().ok()?;
    if health.get("pid").and_then(|x| x.as_u64()) != Some(pid as u64) {
        return None;
    }
    let started_at = v
        .get("started_at")
        .and_then(|x| x.as_u64())
        .unwrap_or_else(now_epoch);
    #[cfg(windows)]
    let job = attach_job(pid);
    append_log(
        log_path,
        &format!("adopted healthy engine pid={pid} port={port}"),
    );
    Some(Engine {
        port,
        pid,
        started_at,
        data_dir: data_dir.to_path_buf(),
        log_path: log_path.to_path_buf(),
        adopted: true,
        stopped: AtomicBool::new(false),
        child: Arc::new(Mutex::new(None)),
        #[cfg(windows)]
        job: Mutex::new(job),
    })
}

#[cfg(windows)]
fn create_kill_job() -> Option<JobHandle> {
    use windows::core::PCWSTR;
    unsafe {
        let job = CreateJobObjectW(None, PCWSTR::null()).ok()?;
        let mut info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION::default();
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        let r = SetInformationJobObject(
            job,
            JobObjectExtendedLimitInformation,
            &info as *const _ as *const core::ffi::c_void,
            std::mem::size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
        );
        if r.is_err() {
            let _ = CloseHandle(job);
            return None;
        }
        Some(JobHandle(job))
    }
}

/// 复用一个已有进程时，把它挂进新的 Job；之后壳死掉同样会被回收。
#[cfg(windows)]
fn attach_job(pid: u32) -> Option<JobHandle> {
    let job = create_kill_job()?;
    unsafe {
        let h = OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, false, pid).ok()?;
        let r = AssignProcessToJobObject(job.0, h);
        let _ = CloseHandle(h);
        if r.is_err() {
            return None;
        }
    }
    Some(job)
}

fn engine_command(app: &AppHandle) -> Result<(String, Vec<String>, PathBuf), String> {
    // 1) 显式覆盖：spike / 调试用
    if let Ok(exe) = std::env::var("LIVENOTES_ENGINE_EXE") {
        let exe = PathBuf::from(exe);
        if exe.is_file() {
            let cwd = exe.parent().unwrap_or(Path::new(".")).to_path_buf();
            return Ok((exe.to_string_lossy().into_owned(), vec![], cwd));
        }
    }
    // 2) 打包态：资源目录里的 onedir 引擎。
    //    Tauri 在 Windows 上把 bundle.resources 原样拷到 exe 目录，配置里写的是
    //    `resources/**/*`，所以实际落点是 `<exe_dir>/resources/engine/engine.exe`；
    //    同时接受 `<exe_dir>/engine`，便于手工把 resources 铺平或做本地测试。
    if let Some(base) = resource_base(app) {
        let exe = base.join("engine").join("engine.exe");
        if exe.is_file() {
            return Ok((exe.to_string_lossy().into_owned(), vec![], base.join("engine")));
        }
    }
    // 3) 开发态：仓库根的 run.py
    let root = Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .and_then(Path::parent)
        .ok_or_else(|| "cannot resolve repo root from CARGO_MANIFEST_DIR".to_string())?
        .to_path_buf();
    let run_py = root.join("run.py");
    if run_py.is_file() {
        let python = std::env::var("LIVENOTES_PYTHON").unwrap_or_else(|_| "python".to_string());
        let mut args = vec![run_py.to_string_lossy().into_owned()];
        // 开发态没有「打包时复制 overlay」这一步，直接把仓库里的桌面皮肤目录
        // 传给引擎；静态资源按 overlay 优先返回（见 app/webroot.py）。
        let overlay = root.join("desktop").join("web");
        if overlay.is_dir() {
            args.push("--web-overlay".to_string());
            args.push(overlay.to_string_lossy().into_owned());
        }
        return Ok((python, args, root));
    }
    Err(format!(
        "找不到引擎：既没有 LIVENOTES_ENGINE_EXE，也没有打包资源 engine/engine.exe，\
         开发仓库里也没有 {}",
        run_py.display()
    ))
}

/// Tauri 的 resource 根目录。Windows 上是 exe 所在目录；`bundle.resources`
/// 里的路径原样保留（配置写的是 `resources/**/*`），所以先看 `resources/` 子目录。
fn resource_base(app: &AppHandle) -> Option<PathBuf> {
    let res = app.path().resource_dir().ok()?;
    let nested = res.join("resources");
    if nested.is_dir() {
        Some(nested)
    } else {
        Some(res)
    }
}

pub fn app_data_root() -> Result<PathBuf, String> {
    // 开发/沙箱验证时可把整个数据根挪到可写目录；打包态不设置，仍用 LOCALAPPDATA。
    if let Some(home) = std::env::var_os("LIVENOTES_HOME") {
        return Ok(PathBuf::from(home));
    }
    let base = std::env::var_os("LOCALAPPDATA")
        .map(PathBuf::from)
        .or_else(|| {
            std::env::var_os("USERPROFILE").map(|p| PathBuf::from(p).join("AppData/Local"))
        })
        .ok_or_else(|| "LOCALAPPDATA is not set".to_string())?;
    Ok(base.join("LiveNotes"))
}

/// WebView2 用户数据目录。默认在数据根下；`LIVENOTES_WEBVIEW_DIR` 可整体覆盖。
/// 每个窗口用独立子目录，避免 room/live/console 的 profile 生命周期互相牵连。
pub fn webview_dir(name: &str) -> Result<PathBuf, String> {
    let base = match std::env::var_os("LIVENOTES_WEBVIEW_DIR") {
        Some(v) => PathBuf::from(v),
        None => app_data_root()?.join("webview2"),
    };
    let dir = base.join(name);
    match std::fs::create_dir_all(&dir) {
        Ok(()) => Ok(dir),
        // 这个环境的沙箱有时对已存在目录返回 ERROR_ALREADY_EXISTS，
        // 只要确认它确实是目录就继续。
        Err(e)
            if e.kind() == std::io::ErrorKind::AlreadyExists && dir.is_dir() =>
        {
            Ok(dir)
        }
        Err(e) => Err(format!("create {}: {e}", dir.display())),
    }
}

fn pick_port() -> Result<u16, String> {
    let listener = TcpListener::bind("127.0.0.1:0")
        .map_err(|e| format!("bind loopback ephemeral port: {e}"))?;
    let port = listener
        .local_addr()
        .map_err(|e| format!("read ephemeral port: {e}"))?
        .port();
    drop(listener);
    Ok(port)
}

/// 诊断页「结束残留引擎」用：只认 python.exe / engine.exe，避免 PID 复用时误杀。
pub fn is_engine_process(pid: u32) -> bool {
    let mut cmd = Command::new("tasklist");
    cmd.args(["/FI", &format!("PID eq {pid}"), "/FO", "CSV", "/NH"]);
    #[cfg(windows)]
    cmd.creation_flags(CREATE_NO_WINDOW);
    let Ok(out) = cmd.output() else {
        return false;
    };
    let text = String::from_utf8_lossy(&out.stdout).to_lowercase();
    text.contains("python.exe") || text.contains("engine.exe")
}

pub fn terminate_pid(pid: u32) {
    let _ = terminate_tree(pid, true);
}

fn terminate_tree(pid: u32, force: bool) -> std::io::Result<()> {
    let mut cmd = Command::new("taskkill");
    cmd.args(["/PID", &pid.to_string(), "/T"]);
    if force {
        cmd.arg("/F");
    }
    #[cfg(windows)]
    cmd.creation_flags(CREATE_NO_WINDOW);
    let _ = cmd.stdout(Stdio::null()).stderr(Stdio::null()).status();
    Ok(())
}

pub fn append_log(path: &Path, msg: &str) {
    // 主线程和 chunk worker 都会写 shell.log；没有锁时两行会互相插进对方中间。
    static LOG_LOCK: LazyLock<Mutex<()>> = LazyLock::new(|| Mutex::new(()));
    let _guard = LOG_LOCK.lock().unwrap_or_else(|e| e.into_inner());
    let ts = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    if let Ok(mut f) = OpenOptions::new().create(true).append(true).open(path) {
        let _ = writeln!(f, "[{ts}] {msg}");
    }
}

pub fn tail_file(path: &Path, max: usize) -> String {
    let Ok(mut f) = File::open(path) else {
        return String::new();
    };
    let len = f.metadata().map(|m| m.len()).unwrap_or(0);
    let start = len.saturating_sub(max as u64);
    let _ = f.seek(SeekFrom::Start(start));
    let mut buf = Vec::new();
    let _ = f.read_to_end(&mut buf);
    String::from_utf8_lossy(&buf).into_owned()
}
