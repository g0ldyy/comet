use crate::cache::{SegmentCacheKey, SegmentLease, VerifiedSegment, VerifiedSegmentCache};
use crate::materialization::ImmutableFileIdentity;
use crate::raw_composite::{RawCompositeBacking, RawCompositePart, RawCompositeSource};
use crate::session::{RandomAccessSession, SessionPosting};
use crate::yenc::DecodedPart;
use serde::Deserialize;
use std::collections::HashMap;
use std::fs;
use std::hint::black_box;
use std::io::{Seek, SeekFrom, Write};
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::{Arc, Mutex};
use std::time::{Instant, SystemTime, UNIX_EPOCH};

const BASELINES: &str = include_str!("../benchmarks/usenet-baselines.json");

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct Baselines {
    regression_threshold_percent: u64,
    warmup_samples: usize,
    measured_samples: usize,
    architectures: HashMap<String, HashMap<String, Baseline>>,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct Baseline {
    unit: String,
    baseline_ns_per_unit: u64,
}

struct TemporaryDirectory(PathBuf);

impl TemporaryDirectory {
    fn new(label: &str) -> Self {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("system time")
            .as_nanos();
        let path = std::env::temp_dir().join(format!(
            "comet-quality-{label}-{}-{nonce}",
            std::process::id()
        ));
        fs::create_dir(&path).expect("create quality directory");
        fs::set_permissions(&path, fs::Permissions::from_mode(0o700))
            .expect("secure quality directory");
        Self(path)
    }
}

impl Drop for TemporaryDirectory {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

fn benchmark<F>(name: &str, unit: &str, units_per_sample: u64, mut operation: F)
where
    F: FnMut(),
{
    let baselines: Baselines = serde_json::from_str(BASELINES).expect("valid benchmark baselines");
    let baseline = &baselines.architectures[std::env::consts::ARCH][name];
    assert_eq!(baseline.unit, unit);

    for _ in 0..baselines.warmup_samples {
        operation();
    }
    let mut samples = Vec::with_capacity(baselines.measured_samples);
    for _ in 0..baselines.measured_samples {
        let started = Instant::now();
        operation();
        samples.push(
            u64::try_from(started.elapsed().as_nanos()).expect("duration fits") / units_per_sample,
        );
    }
    samples.sort_unstable();
    let p95_index = (samples.len() * 95).div_ceil(100) - 1;
    let p95 = samples[p95_index];
    let median = samples[samples.len() / 2];
    let maximum =
        baseline.baseline_ns_per_unit * (100 + baselines.regression_threshold_percent) / 100;
    println!(
        "USENET_BENCH name={name} arch={} unit={unit} median_ns={median} p95_ns={p95} baseline_ns={} maximum_ns={maximum}",
        std::env::consts::ARCH,
        baseline.baseline_ns_per_unit,
    );
    if std::env::var_os("USENET_BENCH_REPORT_ONLY").is_none() {
        assert!(
            p95 <= maximum,
            "{name} p95 {p95} ns/{unit} exceeds the {}% regression ceiling {maximum}",
            baselines.regression_threshold_percent,
        );
    }
}

fn segment(begin: u64, length: usize, total_size: u64, value: u8) -> VerifiedSegment {
    VerifiedSegment::from_decoded(DecodedPart {
        bytes: vec![value; length],
        begin,
        end: begin + u64::try_from(length).unwrap() - 1,
        total_size,
        expected_crc32: Some(u32::from(value)),
        expected_whole_crc32: None,
    })
    .expect("valid benchmark segment")
}

fn nzb_document() -> Vec<u8> {
    let mut document = br#"<?xml version="1.0"?><nzb>"#.to_vec();
    for file in 0..32 {
        document.extend_from_slice(
            format!(
                r#"<file poster="benchmark" date="1" subject="Release.{file}.mkv"><groups><group>alt.binaries.test</group></groups><segments>"#
            )
            .as_bytes(),
        );
        for segment in 1..=8 {
            document.extend_from_slice(
                format!(
                    r#"<segment bytes="1048576" number="{segment}">benchmark-{file}-{segment}@example.invalid</segment>"#
                )
                .as_bytes(),
            );
        }
        document.extend_from_slice(b"</segments></file>");
    }
    document.extend_from_slice(b"</nzb>");
    document
}

#[test]
#[ignore = "run through scripts/run_usenet_benchmarks.sh"]
fn quality_benchmark_nzb_parse() {
    let document = nzb_document();
    benchmark("nzb_parse", "document", 20, || {
        for _ in 0..20 {
            black_box(crate::nzb::parse(black_box(&document))).expect("parse benchmark NZB");
        }
    });
}

fn yenc_article(bytes: &[u8]) -> Vec<u8> {
    let mut article =
        format!("=ybegin line=128 size={} name=benchmark\r\n", bytes.len()).into_bytes();
    for byte in bytes {
        let encoded = byte.wrapping_add(42);
        if matches!(encoded, 0 | b'\n' | b'\r' | b'=') {
            article.push(b'=');
            article.push(encoded.wrapping_add(64));
        } else {
            article.push(encoded);
        }
    }
    article.extend_from_slice(
        format!(
            "\r\n=yend size={} crc32={:08x}\r\n",
            bytes.len(),
            crc32fast::hash(bytes)
        )
        .as_bytes(),
    );
    article
}

#[test]
#[ignore = "run through scripts/run_usenet_benchmarks.sh"]
fn quality_benchmark_yenc_decode() {
    let source = (0..1024 * 1024)
        .map(|index| (index % 251) as u8)
        .collect::<Vec<_>>();
    let article = yenc_article(&source);
    benchmark(
        "yenc_decode",
        "byte",
        u64::try_from(source.len()).unwrap() * 4,
        || {
            for _ in 0..4 {
                let decoded =
                    black_box(crate::yenc::decode(black_box(&article))).expect("decode yEnc");
                assert_eq!(decoded.bytes.len(), source.len());
            }
        },
    );
}

fn cache_keys(count: usize) -> Vec<SegmentCacheKey> {
    (0..count)
        .map(|index| {
            SegmentCacheKey::acquisition(
                [7; 32],
                "benchmark-generation",
                "benchmark-provider",
                &format!("{index}@example.invalid"),
            )
        })
        .collect()
}

#[test]
#[ignore = "run through scripts/run_usenet_benchmarks.sh"]
fn quality_benchmark_cache_hit_and_miss() {
    let keys = cache_keys(256);
    let missing = cache_keys(512).into_iter().skip(256).collect::<Vec<_>>();
    let mut cache = VerifiedSegmentCache::new(8 * 1024 * 1024).unwrap();
    for (index, key) in keys.iter().copied().enumerate() {
        assert_eq!(
            cache.insert(key, segment(1, 4096, 4096, index as u8)),
            crate::cache::Admission::Admitted
        );
    }
    benchmark("cache_hit", "lookup", 4096, || {
        for index in 0..4096 {
            let lease = cache.get(keys[index % keys.len()]).expect("cache hit");
            black_box(lease.segment().bytes().len());
        }
    });
    benchmark("cache_miss", "lookup", 4096, || {
        for index in 0..4096 {
            assert!(black_box(cache.get(missing[index % missing.len()])).is_none());
        }
    });
}

fn session_fixture() -> (
    Arc<Mutex<RandomAccessSession>>,
    HashMap<u64, Arc<VerifiedSegment>>,
) {
    const SEGMENT_BYTES: usize = 4096;
    const SEGMENTS: u64 = 128;
    let total_size = SEGMENTS * SEGMENT_BYTES as u64;
    let postings = (1..=SEGMENTS)
        .map(|number| SessionPosting {
            number,
            declared_encoded_bytes: SEGMENT_BYTES as u64,
            message_id: format!("{number}@example.invalid"),
            fallback_postings: Vec::new(),
        })
        .collect::<Vec<_>>();
    let segments = (1..=SEGMENTS)
        .map(|number| {
            let begin = (number - 1) * SEGMENT_BYTES as u64 + 1;
            (
                number,
                Arc::new(segment(begin, SEGMENT_BYTES, total_size, number as u8)),
            )
        })
        .collect::<HashMap<_, _>>();
    let session =
        RandomAccessSession::new("A".repeat(22), postings, &segments[&1]).expect("session");
    (Arc::new(Mutex::new(session)), segments)
}

#[test]
#[ignore = "run through scripts/run_usenet_benchmarks.sh"]
fn quality_benchmark_random_seek() {
    const REPETITIONS: u64 = 16;
    let (session, segments) = session_fixture();
    let offsets = (0..512)
        .map(|index| ((index * 7919) % (128 * 4096 - 1024)) as u64)
        .collect::<Vec<_>>();
    benchmark(
        "random_seek",
        "seek",
        offsets.len() as u64 * REPETITIONS,
        || {
            for _ in 0..REPETITIONS {
                for offset in &offsets {
                    let bytes = RandomAccessSession::read_at(&session, *offset, 1024, |posting| {
                        Ok(SegmentLease::detached(Arc::clone(
                            &segments[&posting.number],
                        )))
                    })
                    .expect("random seek");
                    black_box(bytes);
                }
            }
        },
    );
}

#[test]
#[ignore = "run through scripts/run_usenet_benchmarks.sh"]
fn quality_benchmark_raw_composite_seek() {
    const PART_BYTES: u64 = 4096;
    const PARTS: u64 = 128;
    const READ_BYTES: u64 = 1024;
    const REPETITIONS: u64 = 16;
    let source = RawCompositeSource::from_ranges(
        "f".repeat(64),
        (0..PARTS)
            .map(|index| RawCompositePart {
                content_identity: format!("{index:064x}"),
                source_offset: 0,
                exact_size: PART_BYTES,
                backing: RawCompositeBacking::Materialization(ImmutableFileIdentity {
                    device: 1,
                    inode: index + 1,
                    size: PART_BYTES,
                    mode: 0o100400,
                    links: 1,
                    modified_seconds: 1,
                    modified_nanoseconds: 0,
                    changed_seconds: 1,
                    changed_nanoseconds: 0,
                }),
            })
            .collect(),
    )
    .expect("raw composite benchmark source");
    let offsets = (0..512)
        .map(|index| (index * 7919) % (PARTS * PART_BYTES - READ_BYTES))
        .collect::<Vec<_>>();
    benchmark(
        "raw_composite_seek",
        "seek",
        offsets.len() as u64 * REPETITIONS,
        || {
            for _ in 0..REPETITIONS {
                for offset in &offsets {
                    let bytes = source
                        .read_at(*offset, READ_BYTES, &|| false, |_, start, end| {
                            Ok(vec![0; usize::try_from(end - start + 1).unwrap()])
                        })
                        .expect("raw composite seek");
                    black_box(bytes);
                }
            }
        },
    );
}

fn vint(value: u64) -> Vec<u8> {
    if value < 0x80 {
        return vec![value as u8];
    }
    let mut bytes = value.to_le_bytes().to_vec();
    while bytes.last() == Some(&0) {
        bytes.pop();
    }
    let mut output = vec![0x80 | bytes.len() as u8];
    output.extend(bytes);
    output
}

fn rar5_block(kind: u64, flags: u64, body: &[u8], data: &[u8]) -> Vec<u8> {
    let mut header = vint(kind);
    header.extend(vint(flags | if data.is_empty() { 0 } else { 0x0002 }));
    if !data.is_empty() {
        header.extend(vint(data.len() as u64));
    }
    header.extend_from_slice(body);
    let size = vint(header.len() as u64);
    let mut output = vec![0; 4];
    output.extend_from_slice(&size);
    output.extend_from_slice(&header);
    let crc = crc32fast::hash(&output[4..]);
    output[..4].copy_from_slice(&crc.to_le_bytes());
    output.extend_from_slice(data);
    output
}

fn rar5_archive() -> Vec<u8> {
    let mut archive = b"Rar!\x1a\x07\x01\0".to_vec();
    archive.extend(rar5_block(1, 0, &vint(0), &[]));
    let data = b"DATA";
    let name = "Movie.2026.mkv";
    let mut body = vint(0x0004);
    body.extend(vint(data.len() as u64));
    body.extend(vint(0));
    body.extend_from_slice(&crc32fast::hash(data).to_le_bytes());
    body.extend(vint(0));
    body.extend(vint(1));
    body.extend(vint(name.len() as u64));
    body.extend_from_slice(name.as_bytes());
    archive.extend(rar5_block(2, 0, &body, data));
    archive.extend(rar5_block(5, 0, &vint(0), &[]));
    archive
}

#[test]
#[ignore = "run through scripts/run_usenet_benchmarks.sh"]
fn quality_benchmark_stored_archive_mapping() {
    let archive = rar5_archive();
    let sizes = [archive.len() as u64];
    benchmark("stored_archive_mapping", "archive", 100, || {
        for _ in 0..100 {
            let members = crate::rar_stored::parse_rar5_stored_members(
                &sizes,
                |_volume_index, offset, length| {
                    let start = usize::try_from(offset).unwrap();
                    Ok(archive[start..start + length].to_vec())
                },
            )
            .expect("map stored archive");
            assert_eq!(members.len(), 1);
            black_box(members);
        }
    });
}

fn required_path(variable: &str) -> PathBuf {
    let path = PathBuf::from(std::env::var_os(variable).expect("quality dependency environment"));
    assert!(path.is_absolute(), "{variable} must be absolute");
    path
}

#[test]
#[ignore = "run through scripts/run_usenet_benchmarks.sh"]
fn quality_benchmark_compressed_materialization() {
    let directory = TemporaryDirectory::new("archive");
    let source = directory.0.join("Movie.2026.mkv");
    let mut state = 0x6a09_e667_f3bc_c909_u64;
    let bytes = (0..1024 * 1024)
        .map(|_| {
            state ^= state << 13;
            state ^= state >> 7;
            state ^= state << 17;
            state as u8
        })
        .collect::<Vec<_>>();
    fs::write(&source, bytes).expect("write compressed source");
    let archive = directory.0.join("benchmark.tar.gz");
    let status = Command::new("tar")
        .current_dir(&directory.0)
        .args(["-czf", "benchmark.tar.gz", "Movie.2026.mkv"])
        .status()
        .expect("create compressed archive");
    assert!(status.success());
    fs::set_permissions(&archive, fs::Permissions::from_mode(0o400)).unwrap();
    let runtime =
        crate::archive_runtime::Runtime::validate(&required_path("USENET_LIBARCHIVE_LIBRARY"))
            .expect("validate libarchive");
    let mut sequence = 0_u64;
    benchmark("compressed_materialization", "entry", 1, || {
        sequence += 1;
        let output = directory.0.join(format!("materialized-{sequence}"));
        let extracted = runtime
            .extract_selected(&archive, &output, "Movie.2026.mkv")
            .expect("materialize compressed entry");
        assert_eq!(extracted.size, 1024 * 1024);
        black_box(fs::metadata(&output).unwrap().len());
        fs::remove_file(output).unwrap();
    });
}

fn create_par2_fixture(binary: &Path, directory: &Path) {
    let source = directory.join("Movie.2026.mkv");
    fs::write(&source, vec![0x6b; 256 * 1024]).expect("write PAR2 source");
    let status = Command::new(binary)
        .current_dir(directory)
        .args([
            "create",
            "-q",
            "-q",
            "-r10",
            "-n1",
            "index.par2",
            "Movie.2026.mkv",
        ])
        .status()
        .expect("create PAR2 fixture");
    assert!(status.success());
    for entry in fs::read_dir(directory).unwrap() {
        let path = entry.unwrap().path();
        fs::set_permissions(path, fs::Permissions::from_mode(0o600)).unwrap();
    }
}

#[test]
#[ignore = "run through scripts/run_usenet_benchmarks.sh"]
fn quality_benchmark_par2_parse() {
    let directory = TemporaryDirectory::new("par2-parse");
    let binary = required_path("USENET_PAR2_BINARY");
    create_par2_fixture(&binary, &directory.0);
    let volumes = fs::read_dir(&directory.0)
        .unwrap()
        .map(|entry| fs::read(entry.unwrap().path()).unwrap())
        .filter(|bytes| bytes.windows(8).any(|window| window == b"PAR2\0PKT"))
        .collect::<Vec<_>>();
    let inputs = volumes.iter().map(Vec::as_slice).collect::<Vec<_>>();
    benchmark("par2_parse", "catalog", 20, || {
        for _ in 0..20 {
            black_box(crate::par2::discover_recovery_sets(black_box(&inputs)))
                .expect("parse PAR2 benchmark catalog");
        }
    });
}

#[test]
#[ignore = "run through scripts/run_usenet_benchmarks.sh"]
fn quality_benchmark_par2_repair() {
    let directory = TemporaryDirectory::new("par2");
    let binary = required_path("USENET_PAR2_BINARY");
    let tool = crate::par2_tool::Tool::validate(&binary).expect("validate PAR2 tool");
    create_par2_fixture(&binary, &directory.0);
    let source = directory.0.join("Movie.2026.mkv");
    benchmark("par2_repair", "repair", 1, || {
        let mut file = fs::OpenOptions::new().write(true).open(&source).unwrap();
        file.seek(SeekFrom::Start(0)).unwrap();
        file.write_all(&[0; 4096]).unwrap();
        file.sync_data().unwrap();
        drop(file);
        tool.repair(&directory.0, 8 * 1024 * 1024, &|| false)
            .expect("repair PAR2 fixture");
        assert_eq!(fs::metadata(&source).unwrap().len(), 256 * 1024);
    });
}
