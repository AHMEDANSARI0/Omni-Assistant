// Prevents an extra console window on Windows in release builds.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::process::Command;
use std::thread;
use std::time::Duration;

/// Where the Python core serves the Command Center.
///
/// The shell never embeds the UI assets: it loads the exact bytes the local API
/// serves, so the desktop window and a browser tab are the same application with
/// the same token, CSP and API origin.
const UI_ORIGIN: &str = "http://127.0.0.1:8765";

fn core_is_up() -> bool {
    tauri::async_runtime::block_on(async {
        // No extra HTTP crate: a failed TCP connect is enough to know the core is
        // not listening yet, and the window will retry on its own.
        std::net::TcpStream::connect_timeout(
            &"127.0.0.1:8765".parse().expect("static socket address"),
            Duration::from_millis(400),
        )
        .is_ok()
    })
}

/// Starts `pc-agent api serve` if nothing is listening yet.
///
/// The command is explicit and inherited from the environment (the venv on
/// Windows); if it fails the window still opens and shows the API's own
/// "backend unreachable" state instead of a blank white screen.
fn ensure_core() {
    if core_is_up() {
        return;
    }
    let python = std::env::var("OMNI_PYTHON").unwrap_or_else(|_| "python".to_string());
    let _ = Command::new(python)
        .args(["-m", "pc_agent", "api", "serve"])
        .spawn();
    for _ in 0..25 {
        if core_is_up() {
            return;
        }
        thread::sleep(Duration::from_millis(200));
    }
}

fn main() {
    ensure_core();
    tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _argv, _cwd| {
            // Second launch focuses the existing window instead of opening another.
            if let Some(window) = tauri::Manager::get_webview_window(app, "main") {
                let _ = window.set_focus();
            }
        }))
        .run(tauri::generate_context!())
        .expect("failed to start the Omni-Assistant Command Center");
}

#[cfg(test)]
mod tests {
    #[test]
    fn origin_is_loopback_only() {
        assert!(super::UI_ORIGIN.starts_with("http://127.0.0.1:"));
    }
}
