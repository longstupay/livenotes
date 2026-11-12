//! 最小 WebView2 环境探针：不经过 Tauri/wry，只验证
//! `CreateCoreWebView2EnvironmentWithOptions` 在当前令牌下能否成功。
//!
//!     cargo run --bin wv2probe -- <user-data-dir>

use std::sync::mpsc;

use webview2_com::{
    CoreWebView2EnvironmentOptions, CreateCoreWebView2EnvironmentCompletedHandler,
    Microsoft::Web::WebView2::Win32::*,
};
use windows::core::{HSTRING, PCWSTR};
use windows::Win32::System::Com::{CoInitializeEx, COINIT_APARTMENTTHREADED};

fn main() {
    unsafe {
        let hr = CoInitializeEx(None, COINIT_APARTMENTTHREADED);
        println!("CoInitializeEx hr={hr:?}");
    }

    let data_dir = std::env::args().nth(1).unwrap_or_default();
    println!("user data dir = {data_dir:?}");

    let (tx, rx) = mpsc::channel();
    let options = CoreWebView2EnvironmentOptions::default();
    let call = unsafe {
        CreateCoreWebView2EnvironmentWithOptions(
            PCWSTR::null(),
            &HSTRING::from(data_dir.as_str()),
            &ICoreWebView2EnvironmentOptions::from(options),
            &CreateCoreWebView2EnvironmentCompletedHandler::create(Box::new(
                move |error_code, environment| {
                    tx.send((format!("{error_code:?}"), environment.is_some()))
                        .ok();
                    Ok(())
                },
            )),
        )
    };
    match call {
        Ok(()) => println!("CreateCoreWebView2EnvironmentWithOptions call ok"),
        Err(e) => {
            println!("CreateCoreWebView2EnvironmentWithOptions call FAILED: {e:?}");
            return;
        }
    }

    match webview2_com::wait_with_pump(rx) {
        Ok((hr, has_env)) => println!("environment callback: hr={hr} env={has_env}"),
        Err(e) => println!("wait_with_pump error: {e:?}"),
    }
}
