//! 极小的 loopback HTTP 客户端。
//!
//! 壳和引擎之间只有 127.0.0.1 上的明文 HTTP；为了避免把 tokio/hyper/reqwest
//! 整条依赖树拖进壳（也为了 16GB 机器上的链接时间），这里自己发请求。
//! 引擎的 BaseHTTPRequestHandler 每个响应都带 Content-Length 并关闭连接，
//! 所以读到 EOF 就是完整响应。

use std::io::{Read, Write};
use std::net::{TcpStream, ToSocketAddrs};
use std::time::Duration;

pub struct Response {
    pub status: u16,
    pub body: Vec<u8>,
}

impl Response {
    pub fn text(&self) -> String {
        String::from_utf8_lossy(&self.body).into_owned()
    }

    pub fn json(&self) -> Result<serde_json::Value, String> {
        serde_json::from_slice(&self.body).map_err(|e| format!("invalid json: {e}"))
    }
}

pub fn request(
    port: u16,
    method: &str,
    path: &str,
    headers: &[(&str, String)],
    body: Option<&[u8]>,
    timeout: Duration,
) -> Result<Response, String> {
    let addr = ("127.0.0.1", port)
        .to_socket_addrs()
        .map_err(|e| format!("resolve loopback: {e}"))?
        .next()
        .ok_or_else(|| "no loopback address".to_string())?;
    let mut stream = TcpStream::connect_timeout(&addr, timeout)
        .map_err(|e| format!("connect 127.0.0.1:{port}: {e}"))?;
    stream
        .set_read_timeout(Some(timeout))
        .map_err(|e| e.to_string())?;
    stream
        .set_write_timeout(Some(timeout))
        .map_err(|e| e.to_string())?;

    let mut head = format!(
        "{method} {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n"
    );
    for (k, v) in headers {
        head.push_str(k);
        head.push_str(": ");
        head.push_str(v);
        head.push_str("\r\n");
    }
    if let Some(b) = body {
        head.push_str(&format!("Content-Length: {}\r\n", b.len()));
    }
    head.push_str("\r\n");

    stream
        .write_all(head.as_bytes())
        .map_err(|e| format!("write request: {e}"))?;
    if let Some(b) = body {
        stream
            .write_all(b)
            .map_err(|e| format!("write body: {e}"))?;
    }

    let mut buf = Vec::with_capacity(16 * 1024);
    stream
        .read_to_end(&mut buf)
        .map_err(|e| format!("read response: {e}"))?;

    let split = buf
        .windows(4)
        .position(|w| w == b"\r\n\r\n")
        .ok_or_else(|| "malformed http response (no header end)".to_string())?;
    let status = String::from_utf8_lossy(&buf[..split])
        .lines()
        .next()
        .and_then(|line| line.split_whitespace().nth(1))
        .and_then(|s| s.parse::<u16>().ok())
        .ok_or_else(|| "malformed http status line".to_string())?;
    Ok(Response {
        status,
        body: buf[split + 4..].to_vec(),
    })
}

pub fn get(port: u16, path: &str, timeout: Duration) -> Result<Response, String> {
    request(port, "GET", path, &[], None, timeout)
}

pub fn post_json(
    port: u16,
    path: &str,
    value: &serde_json::Value,
    timeout: Duration,
) -> Result<Response, String> {
    let body = serde_json::to_vec(value).map_err(|e| e.to_string())?;
    request(
        port,
        "POST",
        path,
        &[("Content-Type", "application/json".into())],
        Some(&body),
        timeout,
    )
}

pub fn post_bytes(
    port: u16,
    path: &str,
    content_type: &str,
    extra: &[(&str, String)],
    body: &[u8],
    timeout: Duration,
) -> Result<Response, String> {
    let mut headers = vec![("Content-Type", content_type.to_string())];
    headers.extend(extra.iter().cloned());
    request(port, "POST", path, &headers, Some(body), timeout)
}
