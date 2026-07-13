use dialogue_layout_profiler_core::model::{FlushPolicy, FrameObservation, ProfileOpenOptions};
use dialogue_layout_profiler_core::DialogueLayoutProfiler;
use serde_json::Value;
use std::env;
use std::fs::{File, OpenOptions};
use std::io::{self, BufRead, BufWriter, Write};

fn main() {
    if let Err(error) = run() {
        eprintln!("{error}");
        std::process::exit(1);
    }
}

fn run() -> Result<(), Box<dyn std::error::Error>> {
    let args = env::args().collect::<Vec<_>>();
    match args.get(1).map(String::as_str) {
        Some("serve") => serve(&args[2..]),
        _ => {
            eprintln!(
                "usage: dialogue-layout-profiler serve --profile <path> [--profile-id <id>] [--flush-every <n>|--flush-each|--manual-flush] [--debug-log <path>]"
            );
            Ok(())
        }
    }
}

fn serve(args: &[String]) -> Result<(), Box<dyn std::error::Error>> {
    let options = parse_serve_options(args)?;
    let mut debug_log = match options.debug_log_path {
        Some(path) => Some(DebugLog::new(path, options.debug_log_flush_every)?),
        None => None,
    };
    let mut profiler = DialogueLayoutProfiler::open(options.open_options)?;
    let stdin = io::stdin();
    let mut stdout = io::stdout().lock();

    for line in stdin.lock().lines() {
        let line = line?;
        if line.trim().is_empty() {
            continue;
        }

        let request_value: Value = match serde_json::from_str(&line) {
            Ok(value) => value,
            Err(error) => {
                let response = serde_json::json!({
                    "type": "error",
                    "error": "invalid_json",
                    "message": error.to_string()
                });
                write_response(&mut stdout, &mut debug_log, None, &response)?;
                continue;
            }
        };

        let request: ServeRequest = match serde_json::from_value(request_value.clone()) {
            Ok(request) => request,
            Err(error) => {
                let response = serde_json::json!({
                    "type": "error",
                    "error": "invalid_request",
                    "message": error.to_string()
                });
                write_response(&mut stdout, &mut debug_log, Some(&request_value), &response)?;
                continue;
            }
        };

        let response = match request {
            ServeRequest::ObserveFrame { frame } => {
                let prediction = profiler.observe_frame(frame)?;
                serde_json::json!({
                    "type": "prediction",
                    "prediction": prediction
                })
            }
            ServeRequest::LegacyFrame(frame) => {
                let prediction = profiler.observe_frame(frame)?;
                serde_json::json!({
                    "type": "prediction",
                    "prediction": prediction
                })
            }
            ServeRequest::Flush => {
                profiler.flush()?;
                if let Some(debug_log) = &mut debug_log {
                    debug_log.flush()?;
                }
                serde_json::json!({ "type": "flushed" })
            }
            ServeRequest::ExportProfile => {
                serde_json::json!({
                    "type": "profile",
                    "profile": profiler.export_profile()
                })
            }
            ServeRequest::Shutdown => {
                profiler.flush()?;
                let response = serde_json::json!({ "type": "shutdown_ack" });
                write_response(&mut stdout, &mut debug_log, Some(&request_value), &response)?;
                if let Some(debug_log) = &mut debug_log {
                    debug_log.flush()?;
                }
                break;
            }
        };

        write_response(&mut stdout, &mut debug_log, Some(&request_value), &response)?;
    }

    profiler.flush()?;
    if let Some(debug_log) = &mut debug_log {
        debug_log.flush()?;
    }
    Ok(())
}

enum ServeRequest {
    ObserveFrame { frame: FrameObservation },
    LegacyFrame(FrameObservation),
    Flush,
    ExportProfile,
    Shutdown,
}

impl<'de> serde::Deserialize<'de> for ServeRequest {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: serde::Deserializer<'de>,
    {
        let value = Value::deserialize(deserializer)?;
        match value.get("type").and_then(Value::as_str) {
            Some("observe_frame") => {
                let frame_value = value
                    .get("frame")
                    .ok_or_else(|| serde::de::Error::custom("observe_frame requires frame"))?;
                let frame = serde_json::from_value(frame_value.clone())
                    .map_err(serde::de::Error::custom)?;
                Ok(ServeRequest::ObserveFrame { frame })
            }
            Some("flush") => Ok(ServeRequest::Flush),
            Some("export_profile") => Ok(ServeRequest::ExportProfile),
            Some("shutdown") => Ok(ServeRequest::Shutdown),
            Some(other) => Err(serde::de::Error::custom(format!(
                "unknown request type: {other}"
            ))),
            None => {
                let frame = serde_json::from_value(value).map_err(serde::de::Error::custom)?;
                Ok(ServeRequest::LegacyFrame(frame))
            }
        }
    }
}

struct ServeOptions {
    open_options: ProfileOpenOptions,
    debug_log_path: Option<String>,
    debug_log_flush_every: u64,
}

fn parse_serve_options(args: &[String]) -> Result<ServeOptions, Box<dyn std::error::Error>> {
    let mut profile_path = None;
    let mut profile_id = None;
    let mut flush_policy = FlushPolicy::default();
    let mut debug_log_path = None;
    let mut debug_log_flush_every = 100;
    let mut index = 0;

    while index < args.len() {
        match args[index].as_str() {
            "--profile" => {
                index += 1;
                profile_path = args.get(index).cloned();
            }
            "--profile-id" => {
                index += 1;
                profile_id = args.get(index).cloned();
            }
            "--flush-every" => {
                index += 1;
                let n = args
                    .get(index)
                    .ok_or("--flush-every requires a value")?
                    .parse::<u64>()?;
                flush_policy = FlushPolicy::EveryNObservations { n };
            }
            "--flush-each" => {
                flush_policy = FlushPolicy::EveryObservation;
            }
            "--manual-flush" => {
                flush_policy = FlushPolicy::Manual;
            }
            "--debug-log" => {
                index += 1;
                debug_log_path = args.get(index).cloned();
            }
            "--debug-log-flush-every" => {
                index += 1;
                debug_log_flush_every = args
                    .get(index)
                    .ok_or("--debug-log-flush-every requires a value")?
                    .parse::<u64>()?;
            }
            unknown => {
                return Err(format!("unknown argument: {unknown}").into());
            }
        }
        index += 1;
    }

    let profile_path = profile_path.ok_or("--profile is required")?;
    let profile_id = profile_id.unwrap_or_else(|| "default".to_string());

    Ok(ServeOptions {
        open_options: ProfileOpenOptions {
            profile_id,
            profile_path,
            flush_policy,
            config: Default::default(),
        },
        debug_log_path,
        debug_log_flush_every,
    })
}

fn write_response(
    stdout: &mut impl Write,
    debug_log: &mut Option<DebugLog>,
    request: Option<&Value>,
    response: &Value,
) -> Result<(), Box<dyn std::error::Error>> {
    writeln!(stdout, "{}", serde_json::to_string(response)?)?;
    stdout.flush()?;

    if let Some(debug_log) = debug_log {
        let entry = serde_json::json!({
            "request": request,
            "response": response
        });
        debug_log.write_entry(&entry)?;
    }

    Ok(())
}

struct DebugLog {
    writer: BufWriter<File>,
    entries_since_flush: u64,
    flush_every: u64,
}

impl DebugLog {
    fn new(path: String, flush_every: u64) -> io::Result<Self> {
        let file = OpenOptions::new().create(true).append(true).open(path)?;
        Ok(Self {
            writer: BufWriter::new(file),
            entries_since_flush: 0,
            flush_every: flush_every.max(1),
        })
    }

    fn write_entry(&mut self, entry: &Value) -> Result<(), Box<dyn std::error::Error>> {
        writeln!(self.writer, "{}", serde_json::to_string(entry)?)?;
        self.entries_since_flush += 1;
        if self.entries_since_flush >= self.flush_every {
            self.flush()?;
        }
        Ok(())
    }

    fn flush(&mut self) -> io::Result<()> {
        self.writer.flush()?;
        self.entries_since_flush = 0;
        Ok(())
    }
}
