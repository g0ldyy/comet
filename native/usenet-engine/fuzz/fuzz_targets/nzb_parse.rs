#![no_main]

use libfuzzer_sys::fuzz_target;

fuzz_target!(|document: &[u8]| {
    let _ = comet_usenet_engine::nzb::parse(document);
});
