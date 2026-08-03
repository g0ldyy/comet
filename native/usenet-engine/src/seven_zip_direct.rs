use aes::Aes256;
use aes::cipher::{BlockDecrypt, KeyInit, generic_array::GenericArray};
use sevenz_rust::{Archive, Error, SevenZMethod};
use sha2::{Digest, Sha256};
use std::io::{Read, Seek};
use std::sync::Arc;
use zeroize::Zeroizing;

const SIGNATURE_HEADER_BYTES: u64 = 32;
const MAX_AES_CYCLES_POWER: u8 = 24;

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct AesStoredSource {
    pub pack_offset: u64,
    pub pack_size: u64,
    pub plaintext_offset: u64,
    pub key: Arc<Zeroizing<[u8; 32]>>,
    pub initial_vector: [u8; 16],
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) enum StoredSource {
    Plain { offset: u64 },
    Aes(AesStoredSource),
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct StoredMember {
    pub relative_path: String,
    pub exact_size: u64,
    pub source: StoredSource,
}

#[derive(Clone)]
enum FolderSource {
    Plain {
        pack_offset: u64,
    },
    Aes {
        pack_offset: u64,
        pack_size: u64,
        key: Arc<Zeroizing<[u8; 32]>>,
        initial_vector: [u8; 16],
    },
}

impl FolderSource {
    fn member(&self, plaintext_offset: u64) -> Result<StoredSource, &'static str> {
        match self {
            Self::Plain { pack_offset } => pack_offset
                .checked_add(plaintext_offset)
                .map(|offset| StoredSource::Plain { offset })
                .ok_or("archive_header_invalid"),
            Self::Aes {
                pack_offset,
                pack_size,
                key,
                initial_vector,
            } => Ok(StoredSource::Aes(AesStoredSource {
                pack_offset: *pack_offset,
                pack_size: *pack_size,
                plaintext_offset,
                key: Arc::clone(key),
                initial_vector: *initial_vector,
            })),
        }
    }
}

pub(crate) fn parse_stored_members<R: Read + Seek>(
    reader: &mut R,
    exact_size: u64,
    passphrase: Option<&str>,
) -> Result<Vec<StoredMember>, &'static str> {
    let password = passphrase.map(utf16le).unwrap_or_default();
    let archive = Archive::read(reader, exact_size, &password).map_err(archive_error)?;
    let mut folder_offsets = vec![0_u64; archive.folders.len()];
    let mut folder_sources = vec![None; archive.folders.len()];
    let mut resolved_folders = vec![false; archive.folders.len()];
    let mut members = Vec::new();

    for (file_index, file) in archive.files.iter().enumerate() {
        let Some(folder_index) = archive
            .stream_map
            .file_folder_index
            .get(file_index)
            .copied()
            .flatten()
        else {
            continue;
        };
        let plaintext_offset = *folder_offsets
            .get(folder_index)
            .ok_or("archive_header_invalid")?;
        folder_offsets[folder_index] = plaintext_offset
            .checked_add(file.size)
            .ok_or("archive_header_invalid")?;
        if file.is_directory || !file.has_stream || file.size == 0 {
            continue;
        }
        if !resolved_folders[folder_index] {
            folder_sources[folder_index] = folder_source(&archive, folder_index, passphrase)?;
            resolved_folders[folder_index] = true;
        }
        let Some(source) = folder_sources[folder_index].as_ref() else {
            continue;
        };
        let relative_path = crate::archive::normalize_archive_path(&file.name)
            .map_err(|_| "archive_path_invalid")?;
        members.push(StoredMember {
            relative_path,
            exact_size: file.size,
            source: source.member(plaintext_offset)?,
        });
    }
    Ok(members)
}

fn folder_source(
    archive: &Archive,
    folder_index: usize,
    passphrase: Option<&str>,
) -> Result<Option<FolderSource>, &'static str> {
    let folder = archive
        .folders
        .get(folder_index)
        .ok_or("archive_header_invalid")?;
    if folder.packed_streams.len() != 1 {
        return Ok(None);
    }
    let first_pack_stream = *archive
        .stream_map
        .folder_first_pack_stream_index
        .get(folder_index)
        .ok_or("archive_header_invalid")?;
    let pack_offset = SIGNATURE_HEADER_BYTES
        .checked_add(archive.pack_pos)
        .and_then(|offset| {
            offset.checked_add(
                *archive
                    .stream_map
                    .pack_stream_offsets
                    .get(first_pack_stream)?,
            )
        })
        .ok_or("archive_header_invalid")?;
    let pack_size = *archive
        .pack_sizes
        .get(first_pack_stream)
        .ok_or("archive_header_invalid")?;
    let ordered = folder.ordered_coder_iter().collect::<Vec<_>>();
    if ordered.len() == 1 && ordered[0].1.decompression_method_id() == SevenZMethod::ID_COPY {
        return Ok(Some(FolderSource::Plain { pack_offset }));
    }
    let mut aes = None;
    for (_index, coder) in ordered {
        match coder.decompression_method_id() {
            SevenZMethod::ID_COPY => {}
            SevenZMethod::ID_AES256SHA256 if aes.is_none() => aes = Some(coder),
            _ => return Ok(None),
        }
    }
    let Some(coder) = aes else {
        return Ok(None);
    };
    let passphrase = passphrase.ok_or("archive_password_required")?;
    let (salt, initial_vector, cycles_power) = aes_parameters(&coder.properties)?;
    let key = derive_key(passphrase, cycles_power, &salt)?;
    Ok(Some(FolderSource::Aes {
        pack_offset,
        pack_size,
        key: Arc::new(Zeroizing::new(key)),
        initial_vector,
    }))
}

fn aes_parameters(properties: &[u8]) -> Result<(Vec<u8>, [u8; 16], u8), &'static str> {
    let (&first, tail) = properties.split_first().ok_or("archive_header_invalid")?;
    let (&second, values) = tail.split_first().ok_or("archive_header_invalid")?;
    let salt_size = usize::from((first >> 7) & 1) + usize::from(second >> 4);
    let vector_size = usize::from((first >> 6) & 1) + usize::from(second & 0x0f);
    if salt_size > 16 || vector_size > 16 || values.len() != salt_size + vector_size {
        return Err("archive_header_invalid");
    }
    let (salt, vector) = values.split_at(salt_size);
    let mut initial_vector = [0_u8; 16];
    initial_vector[..vector.len()].copy_from_slice(vector);
    Ok((salt.to_vec(), initial_vector, first & 0x3f))
}

fn derive_key(passphrase: &str, cycles_power: u8, salt: &[u8]) -> Result<[u8; 32], &'static str> {
    let password = utf16le(passphrase);
    if cycles_power == 0x3f {
        let mut key = [0_u8; 32];
        let salt_bytes = salt.len().min(key.len());
        key[..salt_bytes].copy_from_slice(&salt[..salt_bytes]);
        let password_bytes = password.len().min(key.len() - salt_bytes);
        key[salt_bytes..salt_bytes + password_bytes].copy_from_slice(&password[..password_bytes]);
        return Ok(key);
    }
    if cycles_power > MAX_AES_CYCLES_POWER {
        return Err("archive_kdf_unsupported");
    }
    let mut digest = Sha256::new();
    let mut counter = [0_u8; 8];
    for _ in 0..(1_u64 << cycles_power) {
        digest.update(salt);
        digest.update(&password);
        digest.update(counter);
        increment_counter(&mut counter);
    }
    Ok(digest.finalize().into())
}

fn increment_counter(counter: &mut [u8; 8]) {
    for byte in counter {
        *byte = byte.wrapping_add(1);
        if *byte != 0 {
            break;
        }
    }
}

fn utf16le(value: &str) -> Vec<u8> {
    value.encode_utf16().flat_map(u16::to_le_bytes).collect()
}

pub(crate) fn decrypt_cbc_blocks(
    key: &[u8; 32],
    initial_vector: &[u8; 16],
    ciphertext: &mut [u8],
) -> Result<(), &'static str> {
    if !ciphertext.len().is_multiple_of(16) {
        return Err("archive_ciphertext_invalid");
    }
    let cipher = Aes256::new(GenericArray::from_slice(key));
    let mut previous = *initial_vector;
    for block in ciphertext.chunks_exact_mut(16) {
        let encrypted = <[u8; 16]>::try_from(&*block).expect("AES chunk length is fixed");
        cipher.decrypt_block(GenericArray::from_mut_slice(block));
        for (byte, previous) in block.iter_mut().zip(previous) {
            *byte ^= previous;
        }
        previous = encrypted;
    }
    Ok(())
}

fn archive_error(error: Error) -> &'static str {
    match error {
        Error::PasswordRequired => "archive_password_required",
        Error::MaybeBadPassword(_) => "archive_password_invalid",
        Error::Io(_, _) => "archive_input_unavailable",
        _ => "archive_direct_unsupported",
    }
}

#[cfg(test)]
mod tests {
    use super::{StoredSource, decrypt_cbc_blocks, parse_stored_members};
    use std::io::Cursor;

    const CONTENT: &[u8] = b"0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ";
    const PLAIN_ARCHIVE: &str = concat!(
        "377abcaf271c0004f17b59133e000000000000004a0000000000000014032da8",
        "303132333435363738396162636465666768696a6b6c6d6e6f70717273747576",
        "7778797a4142434445464748494a4b4c4d4e4f505152535455565758595a0104",
        "060001093e00070b01000101000c3e00080a0149c5a5b6000005011115004d00",
        "6f007600690065002e006d006b0076000000140a0100a234e19ed822dd01150601",
        "002080a4810000",
    );
    const ENCRYPTED_ARCHIVE: &str = concat!(
        "377abcaf271c000450be5056b0000000000000002e00000000000000a740ded0",
        "f442cefeb45102bbe6eea2422b5c55c659ab426b84e0fc881d636efda620545e",
        "871a086371b108d918c8954c64d0862e09062e31666ed5969e2e5e7abb26e772",
        "b4d54b1b96ec7356739aaeb3b8ba63948de5432f4a87a520c6701b29f54d232f",
        "1b2bfa2884f91fce34d03ff2292ed82eee866f33e053d6e3b435cc8562c2b0b2",
        "35dcc81727ed118e10ed9ed37b5f1264618a344626b8cdb4fba4769cd598b6dc",
        "a3f85b96eb1300bfa38a4024034e71bb17064001097000070b0100012406f107",
        "0112530fa60ee0ef51aacb00dfcdc183bc4bdc410c6a0a01b3d1aa680000",
    );

    fn decode_hex(encoded: &str) -> Vec<u8> {
        encoded
            .as_bytes()
            .chunks_exact(2)
            .map(|pair| {
                let digit = |byte| match byte {
                    b'0'..=b'9' => byte - b'0',
                    b'a'..=b'f' => byte - b'a' + 10,
                    _ => panic!("invalid fixture hex"),
                };
                digit(pair[0]) << 4 | digit(pair[1])
            })
            .collect()
    }

    #[test]
    fn maps_plain_copy_members_without_extraction() {
        let archive = decode_hex(PLAIN_ARCHIVE);
        let member = parse_stored_members(&mut Cursor::new(&archive), archive.len() as u64, None)
            .expect("parse copy archive")
            .pop()
            .expect("copy member");
        let StoredSource::Plain { offset } = member.source else {
            panic!("expected a plain source");
        };
        assert_eq!(member.relative_path, "Movie.mkv");
        assert_eq!(member.exact_size, CONTENT.len() as u64);
        assert_eq!(
            &archive[offset as usize..offset as usize + CONTENT.len()],
            CONTENT,
        );
    }

    #[test]
    fn decrypts_unaligned_ranges_from_encrypted_copy_members() {
        let archive = decode_hex(ENCRYPTED_ARCHIVE);
        let member = parse_stored_members(
            &mut Cursor::new(&archive),
            archive.len() as u64,
            Some("correct horse battery staple"),
        )
        .expect("parse encrypted copy archive")
        .pop()
        .expect("encrypted copy member");
        let StoredSource::Aes(source) = member.source else {
            panic!("expected an AES source");
        };
        let start = 7_usize;
        let end = 49_usize;
        let aligned_start = start / 16 * 16;
        let aligned_end = end.div_ceil(16) * 16;
        let cipher_start = source.pack_offset as usize + aligned_start;
        let mut ciphertext =
            archive[cipher_start..source.pack_offset as usize + aligned_end].to_vec();
        let vector = if aligned_start == 0 {
            source.initial_vector
        } else {
            archive[cipher_start - 16..cipher_start]
                .try_into()
                .expect("previous cipher block")
        };
        decrypt_cbc_blocks(&source.key, &vector, &mut ciphertext).expect("decrypt selected blocks");
        assert_eq!(
            &ciphertext[start - aligned_start..end - aligned_start],
            &CONTENT[start..end],
        );
    }

    #[test]
    fn reports_an_invalid_password_precisely() {
        let archive = decode_hex(ENCRYPTED_ARCHIVE);
        assert_eq!(
            parse_stored_members(
                &mut Cursor::new(&archive),
                archive.len() as u64,
                Some("wrong password"),
            ),
            Err("archive_password_invalid"),
        );
    }
}
