#![no_main]

use libfuzzer_sys::fuzz_target;

fuzz_target!(|article: &[u8]| {
    let _ = comet_usenet_engine::yenc::decode(article);
});
