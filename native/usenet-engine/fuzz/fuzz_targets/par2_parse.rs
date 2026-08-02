#![no_main]

use comet_usenet_engine::par2::{
    discover_recovery_sets, discover_recovery_sets_from_readers, parse_recovery_set,
    parse_recovery_volumes,
};
use libfuzzer_sys::fuzz_target;
use md5::{Digest, Md5};
use std::io::Cursor;

const PACKET_TYPES: [[u8; 16]; 5] = [
    *b"PAR 2.0\0Main\0\0\0\0",
    *b"PAR 2.0\0FileDesc",
    *b"PAR 2.0\0UniFileN",
    *b"PAR 2.0\0IFSC\0\0\0\0",
    *b"PAR 2.0\0RecvSlic",
];

fn packet(input: &[u8]) -> Vec<u8> {
    let mut body = input.get(1..).unwrap_or_default().to_vec();
    body.resize(body.len().next_multiple_of(4), 0);
    let mut packet = Vec::with_capacity(64 + body.len());
    packet.extend_from_slice(b"PAR2\0PKT");
    packet.extend_from_slice(&(64 + body.len() as u64).to_le_bytes());
    packet.extend_from_slice(&[0; 16]);
    packet.extend_from_slice(&[0; 16]);
    packet.extend_from_slice(&PACKET_TYPES[usize::from(input.first().copied().unwrap_or(0)) % 5]);
    packet.extend_from_slice(&body);
    let digest: [u8; 16] = Md5::digest(&packet[32..]).into();
    packet[16..32].copy_from_slice(&digest);
    packet
}

fuzz_target!(|input: &[u8]| {
    let _ = parse_recovery_set(input);
    let _ = parse_recovery_volumes(&[input]);
    let _ = discover_recovery_sets(&[input]);

    let structured = packet(input);
    let _ = parse_recovery_set(&structured);
    let _ = discover_recovery_sets(&[&structured]);
    let mut reader = [Cursor::new(structured)];
    let _ = discover_recovery_sets_from_readers(&mut reader, &|| false);
});
