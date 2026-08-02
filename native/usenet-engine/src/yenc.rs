use crc32fast::Hasher;

#[derive(Debug, PartialEq)]
pub struct DecodedPart {
    pub bytes: Vec<u8>,
    pub begin: u64,
    pub end: u64,
    pub total_size: u64,
    pub expected_crc32: Option<u32>,
    pub expected_whole_crc32: Option<u32>,
}

pub fn integrity_failure(code: &str) -> bool {
    matches!(
        code,
        "invalid_yenc_header"
            | "invalid_yenc_field"
            | "invalid_yenc_crc"
            | "invalid_yenc_part"
            | "invalid_yenc_range"
            | "missing_ybegin"
            | "missing_yend"
            | "missing_yenc_field"
            | "missing_yenc_crc"
            | "trailing_yenc_data"
            | "truncated_yenc_escape"
            | "yenc_length_mismatch"
            | "yenc_crc_mismatch"
    )
}

fn fields(line: &str) -> impl Iterator<Item = (&str, &str)> {
    line.split_ascii_whitespace()
        .skip(1)
        .scan(false, |finished, value| {
            if *finished {
                return None;
            }
            let field = value.split_once('=');
            if field.is_some_and(|(name, _)| name == "name") {
                *finished = true;
            }
            Some(field)
        })
        .flatten()
}

fn field<'a>(
    line: &'a str,
    key: &str,
    missing: &'static str,
    invalid: &'static str,
) -> Result<&'a str, &'static str> {
    let mut values = fields(line).filter_map(|(name, value)| (name == key).then_some(value));
    let value = values.next().ok_or(missing)?;
    if values.next().is_some() {
        return Err(invalid);
    }
    Ok(value)
}

fn numeric(line: &str, key: &str) -> Result<u64, &'static str> {
    let value = field(line, key, "missing_yenc_field", "invalid_yenc_field")?;
    if value.is_empty() || !value.bytes().all(|byte| byte.is_ascii_digit()) {
        return Err("invalid_yenc_field");
    }
    value.parse().map_err(|_| "invalid_yenc_field")
}

fn optional_numeric(line: &str, key: &str) -> Result<Option<u64>, &'static str> {
    let mut values = fields(line).filter_map(|(name, value)| (name == key).then_some(value));
    let Some(value) = values.next() else {
        return Ok(None);
    };
    if values.next().is_some()
        || value.is_empty()
        || !value.bytes().all(|byte| byte.is_ascii_digit())
    {
        return Err("invalid_yenc_field");
    }
    value.parse().map(Some).map_err(|_| "invalid_yenc_field")
}

fn checksum(line: &str, key: &str) -> Result<u32, &'static str> {
    let value = field(line, key, "missing_yenc_crc", "invalid_yenc_crc")?;
    parse_checksum(value)
}

fn parse_checksum(value: &str) -> Result<u32, &'static str> {
    if value.is_empty() || value.len() > 8 || !value.bytes().all(|byte| byte.is_ascii_hexdigit()) {
        return Err("invalid_yenc_crc");
    }
    u32::from_str_radix(value, 16).map_err(|_| "invalid_yenc_crc")
}

fn optional_checksum(line: &str, key: &str) -> Result<Option<u32>, &'static str> {
    let mut found: Option<&str> = None;
    for (name, value) in fields(line) {
        if name == key {
            if found.is_some() {
                return Err("invalid_yenc_crc");
            }
            found = Some(value);
        }
    }
    match found {
        Some(value) => parse_checksum(value).map(Some),
        None => Ok(None),
    }
}

const MAX_INITIAL_DECODE_CAPACITY: usize = 1024 * 1024;

#[derive(Clone, Copy, Eq, PartialEq)]
enum DecodeState {
    Start,
    Data,
    End,
}

pub struct Decoder {
    state: DecodeState,
    maximum_bytes: usize,
    declared_size: u64,
    begin_part: Option<u64>,
    part_begin: u64,
    part_end: u64,
    is_part: bool,
    decoded: Vec<u8>,
    crc32: Hasher,
    escaped: bool,
    end_line: Option<String>,
}

impl Decoder {
    pub fn new(maximum_bytes: usize) -> Self {
        Self {
            state: DecodeState::Start,
            maximum_bytes,
            declared_size: 0,
            begin_part: None,
            part_begin: 1,
            part_end: 0,
            is_part: false,
            decoded: Vec::new(),
            crc32: Hasher::new(),
            escaped: false,
            end_line: None,
        }
    }

    pub fn push_line(&mut self, line: &[u8]) -> Result<(), &'static str> {
        if line.is_empty() {
            return Ok(());
        }
        match self.state {
            DecodeState::Start => self.begin(line),
            DecodeState::Data if line.starts_with(b"=ypart ") => self.part(line),
            DecodeState::Data if line.starts_with(b"=yend ") => self.end(line),
            DecodeState::Data => self.data(line),
            DecodeState::End => Err("trailing_yenc_data"),
        }
    }

    fn begin(&mut self, line: &[u8]) -> Result<(), &'static str> {
        let header = std::str::from_utf8(line).map_err(|_| "invalid_yenc_header")?;
        if !header.starts_with("=ybegin ") {
            return Err("missing_ybegin");
        }
        self.declared_size = numeric(header, "size")?;
        self.begin_part = optional_numeric(header, "part")?;
        let begin_total = optional_numeric(header, "total")?;
        if self.begin_part.is_some() != begin_total.is_some()
            || self.begin_part.is_some_and(|part| {
                part == 0 || begin_total.is_none_or(|total| total == 0 || part > total)
            })
        {
            return Err("invalid_yenc_part");
        }
        self.part_end = self.declared_size;
        self.state = DecodeState::Data;
        Ok(())
    }

    fn part(&mut self, line: &[u8]) -> Result<(), &'static str> {
        if self.is_part || !self.decoded.is_empty() || self.escaped {
            return Err("invalid_yenc_part");
        }
        let header = std::str::from_utf8(line).map_err(|_| "invalid_yenc_header")?;
        self.part_begin = numeric(header, "begin")?;
        self.part_end = numeric(header, "end")?;
        if self.part_begin == 0
            || self.part_end < self.part_begin
            || self.part_end > self.declared_size
        {
            return Err("invalid_yenc_range");
        }
        self.is_part = true;
        Ok(())
    }

    fn data(&mut self, line: &[u8]) -> Result<(), &'static str> {
        if self.decoded.capacity() == 0 {
            let expected = self
                .part_end
                .checked_sub(self.part_begin)
                .and_then(|value| value.checked_add(1))
                .and_then(|value| usize::try_from(value).ok())
                .unwrap_or(self.maximum_bytes)
                .min(self.maximum_bytes)
                .min(MAX_INITIAL_DECODE_CAPACITY);
            self.decoded.reserve_exact(expected);
        }
        let start = self.decoded.len();
        for byte in line {
            let decoded = if self.escaped {
                self.escaped = false;
                Some(byte.wrapping_sub(64).wrapping_sub(42))
            } else if *byte == b'=' {
                self.escaped = true;
                None
            } else {
                Some(byte.wrapping_sub(42))
            };
            if let Some(decoded) = decoded {
                if self.decoded.len() >= self.maximum_bytes {
                    return Err("article_too_large");
                }
                self.decoded.push(decoded);
            }
        }
        self.crc32.update(&self.decoded[start..]);
        Ok(())
    }

    fn end(&mut self, line: &[u8]) -> Result<(), &'static str> {
        let end_line = std::str::from_utf8(line).map_err(|_| "invalid_yenc_header")?;
        if self.begin_part.is_some() && !self.is_part {
            return Err("invalid_yenc_part");
        }
        if let Some(end_part) = optional_numeric(end_line, "part")?
            && (!self.is_part
                || end_part == 0
                || self.begin_part.is_some_and(|part| part != end_part))
        {
            return Err("invalid_yenc_part");
        }
        if self.escaped {
            return Err("truncated_yenc_escape");
        }
        self.end_line = Some(end_line.to_owned());
        self.state = DecodeState::End;
        Ok(())
    }

    pub fn finish(mut self) -> Result<DecodedPart, &'static str> {
        match self.state {
            DecodeState::Start => return Err("missing_ybegin"),
            DecodeState::Data => return Err("missing_yend"),
            DecodeState::End => {}
        }
        let end_line = self.end_line.take().ok_or("missing_yend")?;
        let expected_length = self
            .part_end
            .checked_sub(self.part_begin)
            .and_then(|value| value.checked_add(1))
            .ok_or("invalid_yenc_range")?;
        if self.decoded.len() as u64 != expected_length
            || numeric(&end_line, "size")? != expected_length
        {
            return Err("yenc_length_mismatch");
        }
        let expected_crc32 = checksum(&end_line, if self.is_part { "pcrc32" } else { "crc32" })?;
        if self.crc32.finalize() != expected_crc32 {
            return Err("yenc_crc_mismatch");
        }
        Ok(DecodedPart {
            bytes: self.decoded,
            begin: self.part_begin,
            end: self.part_end,
            total_size: self.declared_size,
            expected_crc32: Some(expected_crc32),
            expected_whole_crc32: if self.is_part {
                optional_checksum(&end_line, "crc32")?
            } else {
                Some(expected_crc32)
            },
        })
    }
}

pub fn decode(article: &[u8]) -> Result<DecodedPart, &'static str> {
    let mut decoder = Decoder::new(article.len());
    for line in article
        .split(|byte| *byte == b'\n')
        .map(|line| line.strip_suffix(b"\r").unwrap_or(line))
    {
        decoder.push_line(line)?;
    }
    decoder.finish()
}

#[cfg(test)]
mod tests {
    use crc32fast::Hasher;

    use super::{decode, integrity_failure};

    #[test]
    fn decodes_and_checks_a_single_part() {
        let article =
            b"=ybegin line=128 size=3 name=test\r\nklm\r\n=yend size=3 crc32=a3830348\r\n";
        assert_eq!(decode(article).unwrap().bytes, b"ABC");
    }

    #[test]
    fn rejects_corrupt_crc() {
        let article =
            b"=ybegin line=128 size=3 name=test\r\nklm\r\n=yend size=3 crc32=00000000\r\n";
        assert_eq!(decode(article), Err("yenc_crc_mismatch"));
    }

    #[test]
    fn accepts_checksums_with_omitted_leading_zeroes() {
        let article = b"=ybegin line=128 size=1 name=test\r\nL\r\n=yend size=1 crc32=762ae69\r\n";
        assert_eq!(decode(article).unwrap().bytes, b"\"");
    }

    #[test]
    fn rejects_invalid_numeric_and_checksum_fields() {
        assert_eq!(
            decode(b"=ybegin line=128 size=+1 name=test\r\nk\r\n=yend size=1 crc32=d3d99e8b\r\n"),
            Err("invalid_yenc_field")
        );
        assert_eq!(
            decode(b"=ybegin line=128 size=1 name=test\r\nk\r\n=yend size=1 crc32=d3d99e8\r\n"),
            Err("yenc_crc_mismatch")
        );
        assert_eq!(
            decode(b"=ybegin line=128 size=1 name=test\r\nk\r\n=yend size=1 crc32=123456789\r\n"),
            Err("invalid_yenc_crc")
        );
        assert_eq!(
            decode(
                b"=ybegin line=128 size=1 size=2 name=test\r\nk\r\n=yend size=1 crc32=d3d99e8b\r\n"
            ),
            Err("invalid_yenc_field")
        );
        assert_eq!(
            decode(
                b"=ybegin part=2 total=1 line=128 size=2 name=test\r\n\
                  =ypart begin=1 end=1\r\nk\r\n\
                  =yend size=1 part=2 pcrc32=d3d99e8b\r\n"
            ),
            Err("invalid_yenc_part")
        );
        assert_eq!(
            decode(
                b"=ybegin part=1 total=2 line=128 size=2 name=test\r\nk\r\n\
                  =yend size=1 part=1 crc32=d3d99e8b\r\n"
            ),
            Err("invalid_yenc_part")
        );
        assert_eq!(
            decode(
                b"=ybegin line=128 size=1 name=test size=2\r\nk\r\n\
                  =yend size=1 crc32=d3d99e8b\r\n"
            )
            .unwrap()
            .bytes,
            b"A"
        );
    }

    #[test]
    fn decodes_non_utf8_payload_bytes() {
        let mut crc = Hasher::new();
        crc.update(&[0x80]);
        let mut article = b"=ybegin line=128 size=1 name=test\r\n".to_vec();
        article.extend_from_slice(&[0xaa, b'\r', b'\n']);
        article
            .extend_from_slice(format!("=yend size=1 crc32={:08x}\r\n", crc.finalize()).as_bytes());

        assert_eq!(decode(&article).unwrap().bytes, [0x80]);
    }

    #[test]
    fn preserves_an_escape_split_across_wire_lines_without_an_encoded_copy() {
        let mut crc = Hasher::new();
        crc.update(&[19]);
        let article = format!(
            "=ybegin line=1 size=1 name=test\r\n=\r\n}}\r\n\
             =yend size=1 crc32={:08x}\r\n",
            crc.finalize()
        );

        assert_eq!(decode(article.as_bytes()).unwrap().bytes, [19]);
    }

    #[test]
    fn multipart_requires_and_checks_part_crc() {
        let article = b"=ybegin line=128 size=2 name=test\r\n=ypart begin=1 end=1\r\nk\r\n=yend size=1 crc32=00000000\r\n";

        assert_eq!(decode(article), Err("missing_yenc_crc"));
        let valid = b"=ybegin part=1 total=2 line=128 size=2 name=test\r\n=ypart begin=1 end=1\r\nk\r\n=yend size=1 part=1 pcrc32=d3d99e8b\r\n";
        assert_eq!(decode(valid).unwrap().bytes, b"A");
    }

    #[test]
    fn preserves_the_declared_logical_size_and_optional_whole_checksum() {
        let article = b"=ybegin line=128 size=2 name=test\r\n=ypart begin=1 end=1\r\nk\r\n=yend size=1 pcrc32=d3d99e8b crc32=30694c07\r\n";
        let decoded = decode(article).unwrap();

        assert_eq!(decoded.total_size, 2);
        assert_eq!(decoded.expected_whole_crc32, Some(0x30694c07));
    }

    #[test]
    fn rejects_data_after_yend() {
        let article =
            b"=ybegin line=128 size=1 name=test\r\nk\r\n=yend size=1 crc32=d3d99e8b\r\nextra\r\n";
        assert_eq!(decode(article), Err("trailing_yenc_data"));
    }

    #[test]
    fn classifies_every_decoder_failure_as_integrity_evidence() {
        for code in [
            "invalid_yenc_header",
            "invalid_yenc_field",
            "invalid_yenc_crc",
            "invalid_yenc_part",
            "invalid_yenc_range",
            "missing_ybegin",
            "missing_yend",
            "missing_yenc_field",
            "missing_yenc_crc",
            "trailing_yenc_data",
            "truncated_yenc_escape",
            "yenc_length_mismatch",
            "yenc_crc_mismatch",
        ] {
            assert!(integrity_failure(code), "{code}");
        }
        assert!(!integrity_failure("nntp_read_failed"));
    }
}
