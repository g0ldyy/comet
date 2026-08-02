use md5::Md5;
use quick_xml::Reader;
use quick_xml::XmlVersion;
use quick_xml::events::{BytesRef, BytesStart, Event};
use serde::{Deserialize, Serialize};
use sha1::{Digest as _, Sha1};
use sha2::Sha256;
use std::collections::BTreeMap;

pub const MAX_FILES: usize = 20_000;
pub const MAX_SEGMENTS: usize = 2_000_000;
const MAX_STRING_BYTES: usize = 16 * 1024;
const MAX_XML_DEPTH: usize = 32;
const MAX_DECODED_XML_BYTES: usize = crate::limits::MAX_NZB_METADATA_BYTES;
const KNOWN_METADATA: &[&str] = &[
    "category",
    "collection",
    "description",
    "id",
    "name",
    "password",
    "tag",
    "title",
];

pub fn known_metadata(key: &str) -> bool {
    KNOWN_METADATA.contains(&key)
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct Posting {
    pub number: u64,
    pub bytes: u64,
    pub message_id: String,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct File {
    pub subject: Option<String>,
    pub poster: Option<String>,
    pub date: Option<u64>,
    pub groups: Vec<String>,
    pub metadata: BTreeMap<String, String>,
    pub postings: Vec<Posting>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub first_segment_md5: Option<String>,
}

#[derive(Serialize)]
pub struct Manifest {
    pub metadata: BTreeMap<String, String>,
    pub files: Vec<File>,
    pub nh1: String,
    pub nm1: String,
}

fn text(value: &str) -> Result<String, &'static str> {
    if value.len() > MAX_STRING_BYTES || value.bytes().any(|byte| byte.is_ascii_control()) {
        return Err("invalid_nzb_text");
    }
    Ok(value.to_owned())
}

fn optional_text(value: Option<String>) -> Result<Option<String>, &'static str> {
    value
        .filter(|value| !value.is_empty())
        .map(|value| text(&value))
        .transpose()
}

fn append_text(target: &mut String, value: &str) -> Result<(), &'static str> {
    if target
        .len()
        .checked_add(value.len())
        .is_none_or(|length| length > MAX_STRING_BYTES)
    {
        return Err("invalid_nzb_text");
    }
    target.push_str(value);
    Ok(())
}

fn append_reference(target: &mut String, reference: &BytesRef<'_>) -> Result<(), &'static str> {
    if let Some(character) = reference
        .resolve_char_ref()
        .map_err(|_| "invalid_nzb_text")?
    {
        return append_text(target, character.encode_utf8(&mut [0_u8; 4]));
    }
    let decoded = reference.decode().map_err(|_| "invalid_nzb_text")?;
    let value = match decoded.as_ref() {
        "amp" => "&",
        "apos" => "'",
        "gt" => ">",
        "lt" => "<",
        "quot" => "\"",
        _ => return Err("invalid_nzb_text"),
    };
    append_text(target, value)
}

fn skip_xml_whitespace(value: &[u8], cursor: &mut usize) {
    while value.get(*cursor).is_some_and(u8::is_ascii_whitespace) {
        *cursor += 1;
    }
}

fn quoted_doctype_literal(value: &[u8], cursor: &mut usize) -> bool {
    let Some(quote @ (b'\'' | b'"')) = value.get(*cursor).copied() else {
        return false;
    };
    *cursor += 1;
    let Some(length) = value[*cursor..].iter().position(|byte| *byte == quote) else {
        return false;
    };
    *cursor += length + 1;
    true
}

fn is_inert_nzb_doctype(value: &[u8]) -> bool {
    let mut cursor = 0;
    skip_xml_whitespace(value, &mut cursor);
    if value.get(cursor..cursor + 3) != Some(b"nzb") {
        return false;
    }
    cursor += 3;
    if value
        .get(cursor)
        .is_some_and(|byte| !byte.is_ascii_whitespace())
    {
        return false;
    }
    skip_xml_whitespace(value, &mut cursor);
    if cursor == value.len() {
        return true;
    }
    if value.get(cursor..cursor + 6) != Some(b"PUBLIC") {
        return false;
    }
    cursor += 6;
    if !value.get(cursor).is_some_and(u8::is_ascii_whitespace) {
        return false;
    }
    skip_xml_whitespace(value, &mut cursor);
    if !quoted_doctype_literal(value, &mut cursor) {
        return false;
    }
    if !value.get(cursor).is_some_and(u8::is_ascii_whitespace) {
        return false;
    }
    skip_xml_whitespace(value, &mut cursor);
    if !quoted_doctype_literal(value, &mut cursor) {
        return false;
    }
    skip_xml_whitespace(value, &mut cursor);
    cursor == value.len()
}

fn attribute(
    reader: &Reader<&[u8]>,
    event: &BytesStart<'_>,
    name: &[u8],
) -> Result<Option<String>, &'static str> {
    for attribute in event.attributes().with_checks(true) {
        let attribute = attribute.map_err(|_| "invalid_nzb_attribute")?;
        if attribute.key.as_ref() == name {
            return attribute
                .decoded_and_normalized_value(XmlVersion::Implicit1_0, reader.decoder())
                .map(|value| Some(value.into_owned()))
                .map_err(|_| "invalid_nzb_attribute");
        }
    }
    Ok(None)
}

fn required_number(
    reader: &Reader<&[u8]>,
    event: &BytesStart<'_>,
    name: &[u8],
) -> Result<u64, &'static str> {
    let value = attribute(reader, event, name)?.ok_or("missing_nzb_attribute")?;
    if value.is_empty() || !value.bytes().all(|byte| byte.is_ascii_digit()) {
        return Err("invalid_nzb_number");
    }
    value.parse::<u64>().map_err(|_| "invalid_nzb_number")
}

fn canonical_message_id(value: &str) -> Result<String, &'static str> {
    let value = value.trim_matches(|character: char| {
        character == ' ' || character == '\t' || character == '\r' || character == '\n'
    });
    let bracketed = value.starts_with('<');
    if bracketed != value.ends_with('>') {
        return Err("invalid_message_id");
    }
    let value = if bracketed {
        &value[1..value.len() - 1]
    } else {
        value
    };
    if value.is_empty()
        || value.len() > 996
        || value
            .bytes()
            .any(|byte| byte.is_ascii_control() || matches!(byte, b'<' | b'>'))
    {
        return Err("invalid_message_id");
    }
    Ok(value.to_owned())
}

fn cbor_byte(target: &mut Sha256, value: u8) {
    target.update([value]);
}

fn cbor_major(target: &mut Sha256, major: u8, value: u64) {
    match value {
        0..=23 => cbor_byte(target, (major << 5) | value as u8),
        24..=0xff => target.update([(major << 5) | 24, value as u8]),
        0x100..=0xffff => {
            cbor_byte(target, (major << 5) | 25);
            target.update((value as u16).to_be_bytes());
        }
        0x1_0000..=0xffff_ffff => {
            cbor_byte(target, (major << 5) | 26);
            target.update((value as u32).to_be_bytes());
        }
        _ => {
            cbor_byte(target, (major << 5) | 27);
            target.update(value.to_be_bytes());
        }
    }
}

fn cbor_text(target: &mut Sha256, value: &str) {
    cbor_major(target, 3, value.len() as u64);
    target.update(value.as_bytes());
}

fn cbor_optional_text(target: &mut Sha256, value: &Option<String>) {
    match value {
        Some(value) => cbor_text(target, value),
        None => cbor_byte(target, 0xf6),
    }
}

fn hash_cbor_metadata(target: &mut Sha256, metadata: &BTreeMap<String, String>) {
    let mut metadata = metadata.iter().collect::<Vec<_>>();
    metadata.sort_by(|(left, _), (right, _)| {
        left.len()
            .cmp(&right.len())
            .then_with(|| left.as_bytes().cmp(right.as_bytes()))
    });
    cbor_major(target, 5, metadata.len() as u64);
    for (key, value) in metadata {
        cbor_text(target, key);
        cbor_text(target, value);
    }
}

fn hash_cbor_manifest(target: &mut Sha256, metadata: &BTreeMap<String, String>, files: &[File]) {
    cbor_major(target, 4, 3);
    cbor_major(target, 0, 2);
    hash_cbor_metadata(target, metadata);
    cbor_major(target, 4, files.len() as u64);
    for file in files {
        cbor_major(target, 4, 6);
        cbor_optional_text(target, &file.subject);
        cbor_optional_text(target, &file.poster);
        match file.date {
            Some(value) => cbor_major(target, 0, value),
            None => cbor_byte(target, 0xf6),
        }
        cbor_major(target, 4, file.groups.len() as u64);
        for group in &file.groups {
            cbor_text(target, group);
        }
        hash_cbor_metadata(target, &file.metadata);
        let mut group_count = 0usize;
        let mut scan = 0;
        while scan < file.postings.len() {
            let number = file.postings[scan].number;
            scan += 1;
            group_count += 1;
            while scan < file.postings.len() && file.postings[scan].number == number {
                scan += 1;
            }
        }
        cbor_major(target, 4, group_count as u64);
        let mut idx = 0;
        while idx < file.postings.len() {
            let number = file.postings[idx].number;
            let start = idx;
            idx += 1;
            while idx < file.postings.len() && file.postings[idx].number == number {
                idx += 1;
            }
            cbor_major(target, 4, 2);
            cbor_major(target, 0, number);
            cbor_major(target, 4, (idx - start) as u64);
            if idx - start > 1 {
                let mut order: Vec<usize> = (start..idx).collect();
                order.sort_by(|&a, &b| {
                    file.postings[a]
                        .message_id
                        .as_bytes()
                        .cmp(file.postings[b].message_id.as_bytes())
                        .then(file.postings[a].bytes.cmp(&file.postings[b].bytes))
                });
                for i in order {
                    cbor_major(target, 4, 2);
                    cbor_major(target, 0, file.postings[i].bytes);
                    cbor_text(target, &file.postings[i].message_id);
                }
            } else {
                let posting = &file.postings[start];
                cbor_major(target, 4, 2);
                cbor_major(target, 0, posting.bytes);
                cbor_text(target, &posting.message_id);
            }
        }
    }
}

pub fn manifest_identity(metadata: &BTreeMap<String, String>, files: &[File]) -> String {
    let mut manifest_digest = Sha256::new();
    manifest_digest.update(b"comet-nm1-v2\0");
    hash_cbor_manifest(&mut manifest_digest, metadata, files);
    format!("nm1:{:x}", manifest_digest.finalize())
}

fn decode_document(document: &[u8]) -> Result<Option<Vec<u8>>, &'static str> {
    let (little_endian, encoded) = if let Some(encoded) = document.strip_prefix(b"\xff\xfe") {
        (true, encoded)
    } else if let Some(encoded) = document.strip_prefix(b"\xfe\xff") {
        (false, encoded)
    } else {
        return Ok(None);
    };
    if encoded.len() % 2 != 0 {
        return Err("invalid_nzb_encoding");
    }
    let units = encoded.chunks_exact(2).map(|bytes| {
        if little_endian {
            u16::from_le_bytes([bytes[0], bytes[1]])
        } else {
            u16::from_be_bytes([bytes[0], bytes[1]])
        }
    });
    let mut decoded = Vec::with_capacity((encoded.len() / 2).min(MAX_DECODED_XML_BYTES));
    let mut buffer = [0_u8; 4];
    for character in char::decode_utf16(units) {
        let character = character.map_err(|_| "invalid_nzb_encoding")?;
        let bytes = character.encode_utf8(&mut buffer).as_bytes();
        if decoded
            .len()
            .checked_add(bytes.len())
            .is_none_or(|length| length > MAX_DECODED_XML_BYTES)
        {
            return Err("nzb_too_large");
        }
        decoded.extend_from_slice(bytes);
    }
    Ok(Some(decoded))
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum Element {
    Nzb,
    Head,
    File,
    Groups,
    Group,
    Segments,
    Segment,
    Meta,
    Other,
}

fn element(name: &[u8], parent: Option<Element>) -> Element {
    match (name, parent) {
        (b"nzb", None) => Element::Nzb,
        (b"head", Some(Element::Nzb)) => Element::Head,
        (b"file", Some(Element::Nzb)) => Element::File,
        (b"groups", Some(Element::File)) => Element::Groups,
        (b"group", Some(Element::Groups)) => Element::Group,
        (b"segments", Some(Element::File)) => Element::Segments,
        (b"segment", Some(Element::Segments)) => Element::Segment,
        (b"meta", Some(Element::Head | Element::File)) => Element::Meta,
        _ => Element::Other,
    }
}

pub fn parse(document: &[u8]) -> Result<Manifest, &'static str> {
    let decoded = decode_document(document)?;
    let document = decoded.as_deref().unwrap_or(document);
    let mut reader = Reader::from_reader(document);
    reader.config_mut().trim_text(false);
    let mut buffer = Vec::new();
    let mut files = Vec::new();
    let mut metadata = BTreeMap::new();
    let mut current: Option<File> = None;
    let mut collecting: Option<&'static str> = None;
    let mut meta_key: Option<String> = None;
    let mut content = String::new();
    let mut segment_number = 0;
    let mut segment_bytes = 0;
    let mut segments = 0usize;
    let mut elements = Vec::with_capacity(8);
    let mut saw_declaration = false;
    let mut saw_doctype = false;
    let mut saw_root = false;
    let mut saw_head = false;
    let mut closed_root = false;
    loop {
        let event = reader
            .read_event_into(&mut buffer)
            .map_err(|_| "invalid_nzb_xml")?;
        match event {
            Event::Start(event) => {
                if elements.len() >= MAX_XML_DEPTH || closed_root {
                    return Err("invalid_nzb_xml");
                }
                let mut kind = element(event.local_name().as_ref(), elements.last().copied());
                if elements.is_empty() && kind != Element::Nzb {
                    return Err("invalid_nzb_xml");
                }
                if collecting.is_some() {
                    return Err("invalid_nzb_xml");
                }
                match kind {
                    Element::Nzb => {
                        if saw_root {
                            return Err("invalid_nzb_xml");
                        }
                        saw_root = true;
                    }
                    Element::Head => {
                        if saw_head || !files.is_empty() {
                            return Err("invalid_nzb_xml");
                        }
                        saw_head = true;
                    }
                    Element::File => {
                        if files.len() >= MAX_FILES {
                            return Err("too_many_files");
                        }
                        let date = attribute(&reader, &event, b"date")?
                            .filter(|value| !value.is_empty())
                            .map(|value| value.parse().map_err(|_| "invalid_nzb_date"))
                            .transpose()?;
                        current = Some(File {
                            subject: optional_text(attribute(&reader, &event, b"subject")?)?,
                            poster: optional_text(attribute(&reader, &event, b"poster")?)?,
                            date,
                            groups: Vec::new(),
                            metadata: BTreeMap::new(),
                            postings: Vec::new(),
                            first_segment_md5: None,
                        });
                    }
                    Element::Group => {
                        collecting = Some("group");
                        content.clear();
                    }
                    Element::Segment => {
                        segment_number = required_number(&reader, &event, b"number")?;
                        segment_bytes = required_number(&reader, &event, b"bytes")?;
                        if segment_number == 0
                            || segment_bytes == 0
                            || segment_bytes > crate::limits::MAX_DECLARED_POSTING_BYTES
                        {
                            return Err("invalid_nzb_number");
                        }
                        collecting = Some("segment");
                        content.clear();
                    }
                    Element::Meta => {
                        if let Some(key) =
                            attribute(&reader, &event, b"type")?.filter(|key| known_metadata(key))
                        {
                            meta_key = Some(text(&key)?);
                            collecting = Some("meta");
                            content.clear();
                        } else {
                            kind = Element::Other;
                        }
                    }
                    Element::Groups | Element::Segments | Element::Other => {}
                }
                elements.push(kind);
            }
            Event::Empty(event) => {
                if closed_root || elements.is_empty() || collecting.is_some() {
                    return Err("invalid_nzb_xml");
                }
                match element(event.local_name().as_ref(), elements.last().copied()) {
                    Element::Nzb
                    | Element::Head
                    | Element::File
                    | Element::Group
                    | Element::Segment => {
                        return Err("invalid_nzb_xml");
                    }
                    Element::Meta => {
                        attribute(&reader, &event, b"type")?;
                    }
                    Element::Groups | Element::Segments | Element::Other => {}
                }
            }
            Event::Text(event) => {
                let value = event.decode().map_err(|_| "invalid_nzb_text")?;
                if collecting.is_some() {
                    append_text(&mut content, &value)?;
                } else if !value.trim().is_empty()
                    && elements.last().copied() != Some(Element::Other)
                {
                    return Err("invalid_nzb_xml");
                }
            }
            Event::CData(event) => {
                if collecting.is_some() {
                    append_text(
                        &mut content,
                        &event.decode().map_err(|_| "invalid_nzb_text")?,
                    )?;
                } else if elements.last().copied() != Some(Element::Other) {
                    return Err("invalid_nzb_xml");
                }
            }
            Event::GeneralRef(event) => {
                if collecting.is_some() {
                    append_reference(&mut content, &event)?;
                } else if elements.last().copied() != Some(Element::Other) {
                    return Err("invalid_nzb_xml");
                }
            }
            Event::End(_) => {
                let kind = elements.pop().ok_or("invalid_nzb_xml")?;
                match kind {
                    Element::Group => {
                        if collecting != Some("group") {
                            return Err("invalid_nzb_xml");
                        }
                        let group = text(content.trim())?;
                        if group.is_empty() {
                            return Err("invalid_nzb_group");
                        }
                        let groups = &mut current.as_mut().ok_or("invalid_nzb_xml")?.groups;
                        groups.push(group);
                        collecting = None;
                    }
                    Element::Segment => {
                        if collecting != Some("segment") {
                            return Err("invalid_nzb_xml");
                        }
                        let provider_message_id = content.trim_matches(|character: char| {
                            character == ' '
                                || character == '\t'
                                || character == '\r'
                                || character == '\n'
                        });
                        let message_id = canonical_message_id(&content)?;
                        let file = current.as_mut().ok_or("invalid_nzb_xml")?;
                        if file.first_segment_md5.is_none() {
                            file.first_segment_md5 =
                                Some(format!("{:x}", Md5::digest(provider_message_id.as_bytes())));
                        }
                        file.postings.push(Posting {
                            number: segment_number,
                            bytes: segment_bytes,
                            message_id,
                        });
                        segments += 1;
                        if segments > MAX_SEGMENTS {
                            return Err("too_many_segments");
                        }
                        collecting = None;
                    }
                    Element::Meta => {
                        if collecting != Some("meta") {
                            return Err("invalid_nzb_xml");
                        }
                        if let Some(key) = meta_key.take() {
                            let value = text(content.trim())?;
                            if !value.is_empty() {
                                let target = match elements.last().copied() {
                                    Some(Element::Head) => &mut metadata,
                                    Some(Element::File) => {
                                        &mut current.as_mut().ok_or("invalid_nzb_xml")?.metadata
                                    }
                                    _ => return Err("invalid_nzb_xml"),
                                };
                                if target.insert(key, value).is_some() {
                                    return Err("duplicate_nzb_metadata");
                                }
                            }
                        }
                        collecting = None;
                    }
                    Element::File => {
                        if collecting.is_some() {
                            return Err("invalid_nzb_xml");
                        }
                        let mut file = current.take().ok_or("invalid_nzb_xml")?;
                        if file.postings.is_empty() {
                            return Err("empty_nzb_file");
                        }
                        file.groups
                            .sort_by(|left, right| left.as_bytes().cmp(right.as_bytes()));
                        file.groups.dedup();
                        file.postings.sort_by_key(|posting| posting.number);
                        files.push(file);
                    }
                    Element::Nzb => {
                        if !elements.is_empty() {
                            return Err("invalid_nzb_xml");
                        }
                        closed_root = true;
                    }
                    Element::Head | Element::Groups | Element::Segments | Element::Other => {}
                }
            }
            Event::Decl(_) => {
                if saw_declaration || saw_doctype || saw_root || !elements.is_empty() {
                    return Err("invalid_nzb_xml");
                }
                saw_declaration = true;
            }
            Event::DocType(event)
                if !saw_doctype
                    && !saw_root
                    && elements.is_empty()
                    && is_inert_nzb_doctype(event.as_ref()) =>
            {
                saw_doctype = true;
            }
            Event::DocType(_) => return Err("nzb_doctype_forbidden"),
            Event::Eof => break,
            Event::Comment(_) | Event::PI(_) => {}
        }
        buffer.clear();
    }
    if !saw_root || !closed_root || !elements.is_empty() || current.is_some() || files.is_empty() {
        return Err("empty_nzb");
    }
    let mut message_ids = files
        .iter()
        .flat_map(|file| {
            file.postings
                .iter()
                .map(|posting| posting.message_id.as_str())
        })
        .collect::<Vec<_>>();
    message_ids.sort_by(|left, right| left.as_bytes().cmp(right.as_bytes()));
    message_ids.dedup();
    let mut posting_digest = Sha1::new();
    for message_id in message_ids {
        posting_digest.update(b"<");
        posting_digest.update(message_id.as_bytes());
        posting_digest.update(b">\n");
    }
    let nh1 = format!("nh1:{:x}", posting_digest.finalize());
    let nm1 = manifest_identity(&metadata, &files);
    Ok(Manifest {
        metadata,
        files,
        nh1,
        nm1,
    })
}

#[cfg(test)]
mod tests {
    use super::{MAX_XML_DEPTH, parse};

    const DOCUMENT: &[u8] = br#"<?xml version="1.0"?><nzb><file poster="poster" date="1" subject="release"><groups><group>alt.video</group><group>alt.video</group></groups><segments><segment bytes="4" number="2"> &lt;b@example&gt; </segment><segment bytes="3" number="1">&lt;a@example&gt;</segment></segments></file></nzb>"#;

    #[test]
    fn canonicalizes_postings_and_exact_manifest_identity() {
        let manifest = parse(DOCUMENT).unwrap();
        assert_eq!(manifest.nh1, "nh1:1732caa81c454b8d380642ae7c88acd9aa54a8aa");
        assert_eq!(
            manifest.nm1,
            "nm1:4471bd2dc5b62dda562026b79b8d278a98cac8232be26af32a54f5fbdf3a9a63"
        );
        assert_eq!(manifest.files[0].groups, ["alt.video"]);
        assert_eq!(manifest.files[0].postings[0].message_id, "a@example");
        assert_eq!(
            manifest.files[0].first_segment_md5.as_deref(),
            Some("6504296a7cb6e2c5ac10f19550659248")
        );
    }

    #[test]
    fn preserves_bounded_head_metadata_in_the_manifest_identity() {
        let with_password = parse(
            br#"<nzb>
                <head>
                    <meta type="password"> archive-secret </meta>
                    <meta type="category">Movies</meta>
                    <meta type="future">ignored</meta>
                </head>
                <file><segments><segment bytes="1" number="1">a</segment></segments></file>
            </nzb>"#,
        )
        .expect("parse head metadata");
        let without_password = parse(
            br#"<nzb>
                <head><meta type="category">Movies</meta></head>
                <file><segments><segment bytes="1" number="1">a</segment></segments></file>
            </nzb>"#,
        )
        .expect("parse manifest without password");

        assert_eq!(
            with_password.metadata.get("password").map(String::as_str),
            Some("archive-secret")
        );
        assert_eq!(
            with_password.metadata.get("category").map(String::as_str),
            Some("Movies")
        );
        assert!(!with_password.metadata.contains_key("future"));
        assert_ne!(with_password.nm1, without_password.nm1);
        assert!(matches!(
            parse(
                br#"<nzb><head><meta type="password">one</meta><meta type="password">two</meta></head><file><segments><segment bytes="1" number="1">a</segment></segments></file></nzb>"#
            ),
            Err("duplicate_nzb_metadata")
        ));
    }

    #[test]
    fn rejects_documents_without_the_nzb_root() {
        assert!(matches!(
            parse(b"<file><segment bytes=\"1\" number=\"1\">a</segment></file>"),
            Err("invalid_nzb_xml")
        ));
    }

    #[test]
    fn preserves_duplicate_segments_as_fallback_postings() {
        let manifest = parse(b"<nzb><file><segments><segment bytes=\"1\" number=\"1\">b</segment><segment bytes=\"1\" number=\"1\">a</segment></segments></file></nzb>")
            .expect("parse duplicate fallback postings");
        assert_eq!(
            manifest.files[0]
                .postings
                .iter()
                .map(|posting| (posting.number, posting.message_id.as_str()))
                .collect::<Vec<_>>(),
            [(1, "b"), (1, "a")]
        );
        let reordered = parse(b"<nzb><file><segments><segment bytes=\"1\" number=\"1\">a</segment><segment bytes=\"1\" number=\"1\">b</segment></segments></file></nzb>")
            .expect("parse reordered duplicate fallback postings");
        assert_eq!(manifest.nm1, reordered.nm1);
    }

    #[test]
    fn decodes_utf16_bom_and_stably_orders_cdata_fallbacks() {
        let document = "<?xml version=\"1.0\" encoding=\"UTF-16\"?><nzb><file><segments><segment bytes=\"1\" number=\"2\">two</segment><segment bytes=\"1\" number=\"1\"><![CDATA[<primary>]]></segment><segment bytes=\"1\" number=\"1\"><![CDATA[<fallback>]]></segment></segments></file></nzb>";
        for little_endian in [true, false] {
            let mut encoded = if little_endian {
                vec![0xff, 0xfe]
            } else {
                vec![0xfe, 0xff]
            };
            for unit in document.encode_utf16() {
                let bytes = if little_endian {
                    unit.to_le_bytes()
                } else {
                    unit.to_be_bytes()
                };
                encoded.extend_from_slice(&bytes);
            }

            let manifest = parse(&encoded).expect("parse UTF-16 NZB");

            assert_eq!(
                manifest.files[0]
                    .postings
                    .iter()
                    .map(|posting| (posting.number, posting.message_id.as_str()))
                    .collect::<Vec<_>>(),
                [(1, "primary"), (1, "fallback"), (2, "two")]
            );
        }
        assert!(matches!(parse(b"\xff\xfe<"), Err("invalid_nzb_encoding")));
        assert!(matches!(
            parse(b"\xff\xfe\x00\xd8"),
            Err("invalid_nzb_encoding")
        ));
    }

    #[test]
    fn rejects_unclosed_roots() {
        assert!(matches!(
            parse(b"<nzb><file><segments><segment bytes=\"1\" number=\"1\">a</segment></segments></file>"),
            Err("empty_nzb") | Err("invalid_nzb_xml")
        ));
    }

    #[test]
    fn accepts_namespaced_nzb_and_ignores_unknown_metadata() {
        let document = br#"<n:nzb xmlns:n="urn:nzb"><n:file subject="release"><n:meta type="future">ignored</n:meta><n:meta type="title">kept</n:meta><n:segments><n:segment bytes="1" number="1">&lt;a@example&gt;</n:segment></n:segments></n:file></n:nzb>"#;

        let manifest = parse(document).unwrap();
        assert_eq!(
            manifest.files[0].metadata.get("title"),
            Some(&"kept".to_owned())
        );
        assert!(!manifest.files[0].metadata.contains_key("future"));
    }

    #[test]
    fn ignores_unconsumed_metadata_without_applying_stored_field_limits() {
        let opaque = "x".repeat(super::MAX_STRING_BYTES + 1);
        let document = format!(
            "<nzb><head><meta type=\"{opaque}\">{opaque}</meta><meta type=\"{opaque}\"/><meta type=\"future\"><nested>{opaque}</nested></meta></head><file><segments><segment bytes=\"1\" number=\"1\">a</segment></segments></file></nzb>"
        );

        let manifest = parse(document.as_bytes()).expect("ignore opaque metadata");

        assert!(manifest.metadata.is_empty());
    }

    #[test]
    fn rejects_message_ids_outside_the_nntp_command_domain() {
        for message_id in ["&lt;unclosed", "unopened&gt;", "&lt;nested&lt;id&gt;&gt;"] {
            let document = format!(
                "<nzb><file><segments><segment bytes=\"1\" number=\"1\">{message_id}</segment></segments></file></nzb>"
            );
            assert!(matches!(
                parse(document.as_bytes()),
                Err("invalid_message_id")
            ));
        }
    }

    #[test]
    fn accepts_inert_nzb_doctypes_without_version_allowlists() {
        for version in ["0.9", "1.0", "1.1", "2.0"] {
            let document = format!(
                r#"<!DOCTYPE nzb
                PUBLIC "-//newzBin//DTD NZB {version}//EN"
                "http://www.newzbin.com/DTD/nzb/nzb-{version}.dtd">
                <nzb><file subject="release" date=""><segments>
                <segment bytes="1" number="1">&lt;a@example&gt;</segment>
                </segments></file></nzb>"#
            );
            let manifest = parse(document.as_bytes()).expect("standard NZB doctype");
            assert_eq!(manifest.files.len(), 1);
            assert_eq!(manifest.files[0].date, None);
        }
        parse(
            br#"<!DOCTYPE nzb><nzb><file><segments><segment bytes="1" number="1">a</segment></segments></file></nzb>"#,
        )
        .expect("root-only NZB doctype");
        parse(
            br#"<!DOCTYPE nzb PUBLIC '-//Provider//DTD Future NZB//EN' 'https://provider.example/nzb.dtd'><nzb><file><segments><segment bytes="1" number="1">a</segment></segments></file></nzb>"#,
        )
        .expect("opaque public NZB doctype");

        for forbidden in [
            br#"<!DOCTYPE nzb SYSTEM "http://www.newzbin.com/DTD/nzb/nzb-1.1.dtd"><nzb><file><segments><segment bytes="1" number="1">a</segment></segments></file></nzb>"#.as_slice(),
            br#"<!DOCTYPE nzb PUBLIC "-//newzBin//DTD NZB 1.1//EN" "http://www.newzbin.com/DTD/nzb/nzb-1.1.dtd" [<!ENTITY injected "value">]><nzb><file><segments><segment bytes="1" number="1">a</segment></segments></file></nzb>"#.as_slice(),
            br#"<!DOCTYPE rss PUBLIC "-//newzBin//DTD NZB 1.1//EN" "http://www.newzbin.com/DTD/nzb/nzb-1.1.dtd"><nzb><file><segments><segment bytes="1" number="1">a</segment></segments></file></nzb>"#.as_slice(),
        ] {
            assert!(matches!(
                parse(forbidden),
                Err("nzb_doctype_forbidden")
            ));
        }
    }

    #[test]
    fn rejects_segment_numbers_and_sizes_outside_the_runtime_domain() {
        assert!(matches!(
            parse(b"<nzb><file><segments><segment bytes=\"1\" number=\"+1\">a</segment></segments></file></nzb>"),
            Err("invalid_nzb_number")
        ));

        let maximum = crate::limits::MAX_DECLARED_POSTING_BYTES;
        let valid = format!(
            "<nzb><file><segments><segment bytes=\"{maximum}\" number=\"1\">a</segment></segments></file></nzb>"
        );
        assert_eq!(
            parse(valid.as_bytes())
                .expect("accept the runtime segment-size limit")
                .files[0]
                .postings[0]
                .bytes,
            maximum
        );

        for invalid in [0, maximum + 1] {
            let document = format!(
                "<nzb><file><segments><segment bytes=\"{invalid}\" number=\"1\">a</segment></segments></file></nzb>"
            );
            assert!(matches!(
                parse(document.as_bytes()),
                Err("invalid_nzb_number")
            ));
        }
    }

    #[test]
    fn enforces_bounded_unambiguous_xml_structure() {
        for document in [
            b"<wrapper><nzb><file><segments><segment bytes=\"1\" number=\"1\">a</segment></segments></file></nzb></wrapper>".as_slice(),
            b"<nzb><file><segments><segment bytes=\"1\" number=\"1\">a</segment></segments></file></nzb><extra/>".as_slice(),
            b"<nzb><file><segments><segment bytes=\"1\" number=\"1\">a<extension/>b</segment></segments></file></nzb>".as_slice(),
            b"<nzb><extension><file><segments><segment bytes=\"1\" number=\"1\">hidden</segment></segments></file></extension></nzb>".as_slice(),
        ] {
            assert!(parse(document).is_err());
        }

        let extended = parse(
            br#"<nzb>
                <head><meta type="title">global title</meta></head>
                <extension><file><segments><segment bytes="1" number="1">hidden</segment></segments></file></extension>
                <file subject="visible"><extension>ignored</extension><segments>
                    <segment bytes="1" number="1">visible</segment>
                </segments></file>
            </nzb>"#,
        )
        .expect("ignore extension subtrees without interpreting nested NZB elements");
        assert_eq!(extended.files.len(), 1);
        assert_eq!(extended.files[0].postings[0].message_id, "visible");

        let mut deeply_nested = b"<nzb>".to_vec();
        for _ in 0..MAX_XML_DEPTH {
            deeply_nested.extend_from_slice(b"<extension>");
        }
        assert!(matches!(parse(&deeply_nested), Err("invalid_nzb_xml")));
    }
}
