#![no_main]

use comet_usenet_engine::nntp_protocol::{MultilineBodyDecoder, status_code};
use libfuzzer_sys::fuzz_target;

fuzz_target!(|input: &[u8]| {
    let _ = status_code(input);

    let maximum_bytes = input.first().map_or(0, |value| usize::from(*value) * 64);
    let mut raw_decoder = MultilineBodyDecoder::new(maximum_bytes);
    for wire_line in input.split_inclusive(|byte| *byte == b'\n') {
        if raw_decoder.push_line(wire_line).unwrap_or(true) {
            break;
        }
    }
    let _ = raw_decoder.into_body();

    // A structured pass reaches valid framing states even when mutations do
    // not happen to preserve CRLF.
    let mut framed_decoder = MultilineBodyDecoder::new(maximum_bytes);
    for line in input.split(|byte| *byte == b'\n') {
        let mut wire_line = line.strip_suffix(b"\r").unwrap_or(line).to_vec();
        wire_line.extend_from_slice(b"\r\n");
        if framed_decoder.push_line(&wire_line).unwrap_or(true) {
            break;
        }
    }
    let _ = framed_decoder.into_body();
});
