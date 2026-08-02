//! Pure NNTP response framing used by the socket runtime and fuzz targets.

pub const MAX_LINE_BYTES: usize = 8 * 1024;

pub fn status_code(value: &[u8]) -> Result<u16, &'static str> {
    let bytes = value.get(..3).ok_or("nntp_invalid_response")?;
    if !bytes.iter().all(|byte| byte.is_ascii_digit())
        || !value.ends_with(b"\r\n")
        || (value.len() != 5 && value.get(3) != Some(&b' '))
    {
        return Err("nntp_invalid_response");
    }
    let code = u16::from(bytes[0] - b'0') * 100
        + u16::from(bytes[1] - b'0') * 10
        + u16::from(bytes[2] - b'0');
    (100..=599)
        .contains(&code)
        .then_some(code)
        .ok_or("nntp_invalid_response")
}

pub struct MultilineBodyDecoder {
    body: Vec<u8>,
    maximum_bytes: usize,
    complete: bool,
}

impl MultilineBodyDecoder {
    pub fn new(maximum_bytes: usize) -> Self {
        Self {
            body: Vec::with_capacity(maximum_bytes.min(MAX_LINE_BYTES)),
            maximum_bytes,
            complete: false,
        }
    }

    /// Accept one complete CRLF-terminated NNTP wire line.
    ///
    /// Returns `true` only for the terminating dot line. Dot-stuffing is
    /// removed before the decoded article bytes are appended.
    pub fn push_line(&mut self, line: &[u8]) -> Result<bool, &'static str> {
        if self.complete {
            return Err("nntp_invalid_response");
        }
        let Some(value) = multiline_value(line)? else {
            self.complete = true;
            return Ok(true);
        };
        if self
            .body
            .len()
            .checked_add(value.len())
            .is_none_or(|length| length > self.maximum_bytes)
        {
            return Err("article_too_large");
        }
        self.body.extend_from_slice(value);
        Ok(false)
    }

    pub fn into_body(self) -> Result<Vec<u8>, &'static str> {
        self.complete
            .then_some(self.body)
            .ok_or("nntp_invalid_response")
    }
}

pub fn multiline_value(line: &[u8]) -> Result<Option<&[u8]>, &'static str> {
    if line.is_empty() || line.len() > MAX_LINE_BYTES || !line.ends_with(b"\r\n") {
        return Err("nntp_invalid_response");
    }
    if line == b".\r\n" {
        return Ok(None);
    }
    match line.first() {
        Some(b'.') if line.starts_with(b"..") => Ok(Some(&line[1..])),
        Some(b'.') => Err("nntp_invalid_response"),
        _ => Ok(Some(line)),
    }
}

#[cfg(test)]
mod tests {
    use super::{MultilineBodyDecoder, multiline_value, status_code};

    #[test]
    fn parses_status_and_rejects_short_or_non_numeric_values() {
        assert_eq!(status_code(b"222 body follows\r\n"), Ok(222));
        assert_eq!(status_code(b"222\r\n"), Ok(222));
        assert_eq!(status_code(b"999 extra\r\n"), Err("nntp_invalid_response"));
        assert_eq!(status_code(b"22"), Err("nntp_invalid_response"));
        assert_eq!(status_code(b"2x2"), Err("nntp_invalid_response"));
        assert_eq!(
            status_code(b"222body follows\r\n"),
            Err("nntp_invalid_response")
        );
        assert_eq!(
            status_code(b"222 body follows\n"),
            Err("nntp_invalid_response")
        );
    }

    #[test]
    fn decodes_dot_stuffing_and_requires_a_terminator() {
        let mut decoder = MultilineBodyDecoder::new(32);
        assert_eq!(decoder.push_line(b"..visible\r\n"), Ok(false));
        assert_eq!(decoder.push_line(b"value\r\n"), Ok(false));
        assert_eq!(decoder.push_line(b".\r\n"), Ok(true));
        assert_eq!(decoder.into_body().unwrap(), b".visible\r\nvalue\r\n");

        let mut incomplete = MultilineBodyDecoder::new(8);
        incomplete.push_line(b"x\r\n").unwrap();
        assert_eq!(
            incomplete.into_body(),
            Err::<Vec<u8>, _>("nntp_invalid_response")
        );
    }

    #[test]
    fn enforces_wire_line_and_decoded_body_bounds() {
        let mut decoder = MultilineBodyDecoder::new(2);
        assert_eq!(decoder.push_line(b"x\n"), Err("nntp_invalid_response"));
        assert_eq!(decoder.push_line(b"x\r\n"), Err("article_too_large"));
        let mut noncanonical = MultilineBodyDecoder::new(16);
        assert_eq!(
            noncanonical.push_line(b".visible\r\n"),
            Err("nntp_invalid_response")
        );
    }

    #[test]
    fn exposes_canonical_unstuffed_multiline_values() {
        assert_eq!(
            multiline_value(b"value\r\n"),
            Ok(Some(b"value\r\n".as_slice()))
        );
        assert_eq!(
            multiline_value(b"..visible\r\n"),
            Ok(Some(b".visible\r\n".as_slice()))
        );
        assert_eq!(multiline_value(b".\r\n"), Ok(None));
        assert_eq!(
            multiline_value(b".invalid\r\n"),
            Err("nntp_invalid_response")
        );
    }
}
