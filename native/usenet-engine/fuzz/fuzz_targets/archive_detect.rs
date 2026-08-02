#![no_main]

use comet_usenet_engine::archive::{
    ArchiveFormat, detect_archive, detect_volume_end, normalize_archive_path,
};
use comet_usenet_engine::rar_stored::{
    parse_rar4_stored_members, parse_rar5_stored_members,
};
use libfuzzer_sys::fuzz_target;

fn read(
    input: &[u8],
    volume_index: usize,
    offset: u64,
    length: usize,
) -> Result<Vec<u8>, &'static str> {
    if volume_index != 0 {
        return Err("archive_header_invalid");
    }
    let start = usize::try_from(offset).map_err(|_| "archive_header_invalid")?;
    input
        .get(
            start
                ..start
                    .checked_add(length)
                    .ok_or("archive_header_invalid")?,
        )
        .map(<[u8]>::to_vec)
        .ok_or("archive_header_incomplete")
}

fuzz_target!(|input: &[u8]| {
    let _ = detect_archive(input);
    let _ = detect_volume_end(ArchiveFormat::Rar4, input);
    let _ = detect_volume_end(ArchiveFormat::Rar5, input);
    let sizes = [input.len() as u64];
    let _ = parse_rar4_stored_members(&sizes, |volume, offset, length| {
        read(input, volume, offset, length)
    });
    let _ = parse_rar5_stored_members(&sizes, |volume, offset, length| {
        read(input, volume, offset, length)
    });
    if let Ok(path) = std::str::from_utf8(input) {
        let _ = normalize_archive_path(path);
    }
});
