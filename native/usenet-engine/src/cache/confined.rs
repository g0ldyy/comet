use std::ffi::{CString, OsString};
use std::fs::{File, OpenOptions};
use std::io;
use std::os::fd::{AsRawFd, FromRawFd};
use std::os::unix::fs::{MetadataExt, OpenOptionsExt};
use std::path::{Path, PathBuf};

pub struct ConfinedDirectory {
    path: PathBuf,
    directory: File,
    device: u64,
    inode: u64,
}

impl ConfinedDirectory {
    pub fn open(path: &Path) -> io::Result<Self> {
        let directory = OpenOptions::new()
            .read(true)
            .custom_flags(libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC)
            .open(path)?;
        let metadata = directory.metadata()?;
        Ok(Self {
            path: path.to_path_buf(),
            device: metadata.dev(),
            inode: metadata.ino(),
            directory,
        })
    }

    pub fn open_read(&self, name: &str) -> io::Result<File> {
        self.verify_path_identity()?;
        self.openat2(name, libc::O_RDONLY | libc::O_CLOEXEC, 0)
    }

    pub fn create_new(&self, name: &str, mode: u32) -> io::Result<File> {
        self.verify_path_identity()?;
        self.openat2(
            name,
            libc::O_WRONLY | libc::O_CREAT | libc::O_EXCL | libc::O_CLOEXEC,
            mode,
        )
    }

    pub fn exists(&self, name: &str) -> io::Result<bool> {
        self.verify_path_identity()?;
        let name = component(name)?;
        let mut metadata: libc::stat = unsafe { std::mem::zeroed() };
        let result = unsafe {
            libc::fstatat(
                self.directory.as_raw_fd(),
                name.as_ptr(),
                &mut metadata,
                libc::AT_SYMLINK_NOFOLLOW,
            )
        };
        if result == 0 {
            Ok(true)
        } else {
            let error = io::Error::last_os_error();
            if error.kind() == io::ErrorKind::NotFound {
                Ok(false)
            } else {
                Err(error)
            }
        }
    }

    pub fn hard_link_no_replace(&self, source: &str, target: &str) -> io::Result<()> {
        self.verify_path_identity()?;
        let source = component(source)?;
        let target = component(target)?;
        let result = unsafe {
            libc::linkat(
                self.directory.as_raw_fd(),
                source.as_ptr(),
                self.directory.as_raw_fd(),
                target.as_ptr(),
                0,
            )
        };
        if result == 0 {
            Ok(())
        } else {
            Err(io::Error::last_os_error())
        }
    }

    pub fn remove(&self, name: &str) -> io::Result<()> {
        self.verify_path_identity()?;
        let name = component(name)?;
        let result = unsafe { libc::unlinkat(self.directory.as_raw_fd(), name.as_ptr(), 0) };
        if result == 0 {
            Ok(())
        } else {
            Err(io::Error::last_os_error())
        }
    }

    pub fn entry_names(&self) -> io::Result<Vec<OsString>> {
        self.verify_path_identity()?;
        let names = std::fs::read_dir(&self.path)?
            .map(|entry| entry.map(|entry| entry.file_name()))
            .collect::<Result<Vec<_>, _>>()?;
        self.verify_path_identity()?;
        Ok(names)
    }

    pub fn sync(&self) -> io::Result<()> {
        self.verify_path_identity()?;
        self.directory.sync_all()
    }

    pub fn available_bytes(&self) -> io::Result<u64> {
        self.verify_path_identity()?;
        let mut statistics: libc::statvfs = unsafe { std::mem::zeroed() };
        if unsafe { libc::fstatvfs(self.directory.as_raw_fd(), &mut statistics) } != 0 {
            return Err(io::Error::last_os_error());
        }
        statistics
            .f_bavail
            .checked_mul(statistics.f_frsize)
            .ok_or_else(|| io::Error::other("cache capacity overflow"))
    }

    #[cfg(test)]
    pub(super) fn path(&self) -> &Path {
        &self.path
    }

    fn openat2(&self, name: &str, flags: i32, mode: u32) -> io::Result<File> {
        let name = component(name)?;
        let mut how: libc::open_how = unsafe { std::mem::zeroed() };
        how.flags = flags as u64;
        how.mode = u64::from(mode);
        how.resolve =
            libc::RESOLVE_BENEATH | libc::RESOLVE_NO_MAGICLINKS | libc::RESOLVE_NO_SYMLINKS;
        let descriptor = unsafe {
            libc::syscall(
                libc::SYS_openat2,
                self.directory.as_raw_fd(),
                name.as_ptr(),
                &how,
                std::mem::size_of::<libc::open_how>(),
            )
        };
        if descriptor < 0 {
            Err(io::Error::last_os_error())
        } else {
            Ok(unsafe { File::from_raw_fd(descriptor as i32) })
        }
    }

    fn verify_path_identity(&self) -> io::Result<()> {
        let metadata = std::fs::symlink_metadata(&self.path)?;
        if metadata.dev() != self.device || metadata.ino() != self.inode {
            return Err(io::Error::other("cache root identity changed"));
        }
        Ok(())
    }
}

fn component(value: &str) -> io::Result<CString> {
    if value.is_empty() || value == "." || value == ".." || value.as_bytes().contains(&b'/') {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "invalid confined path component",
        ));
    }
    CString::new(value).map_err(|_| {
        io::Error::new(
            io::ErrorKind::InvalidInput,
            "invalid confined path component",
        )
    })
}

#[cfg(test)]
mod tests {
    use super::ConfinedDirectory;
    use std::io::{Read, Write};
    use std::os::unix::fs::symlink;

    #[test]
    fn opens_only_direct_children_without_following_symlinks() {
        let root = std::env::temp_dir().join(format!(
            "comet-confined-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir(&root).unwrap();
        let outside = root.with_extension("outside");
        std::fs::write(&outside, b"outside").unwrap();
        symlink(&outside, root.join("escape")).unwrap();
        let confined = ConfinedDirectory::open(&root).unwrap();

        assert!(confined.open_read("../outside").is_err());
        assert!(confined.open_read("escape").is_err());
        let mut file = confined.create_new("inside", 0o600).unwrap();
        file.write_all(b"inside").unwrap();
        file.sync_all().unwrap();
        drop(file);
        let mut bytes = Vec::new();
        confined
            .open_read("inside")
            .unwrap()
            .read_to_end(&mut bytes)
            .unwrap();
        assert_eq!(bytes, b"inside");

        let _ = std::fs::remove_dir_all(root);
        let _ = std::fs::remove_file(outside);
    }

    #[test]
    fn detects_when_the_directory_path_is_replaced_after_open() {
        let root = std::env::temp_dir().join(format!(
            "comet-confined-replaced-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        let original = root.with_extension("original");
        std::fs::create_dir(&root).unwrap();
        std::fs::write(root.join("source"), b"source").unwrap();
        let confined = ConfinedDirectory::open(&root).unwrap();
        std::fs::rename(&root, &original).unwrap();
        std::fs::create_dir(&root).unwrap();

        assert!(confined.entry_names().is_err());
        assert!(confined.open_read("source").is_err());
        assert!(confined.create_new("created", 0o600).is_err());
        assert!(confined.exists("source").is_err());
        assert!(confined.hard_link_no_replace("source", "linked").is_err());
        assert!(confined.remove("source").is_err());
        assert!(confined.sync().is_err());
        assert!(confined.available_bytes().is_err());
        assert!(original.join("source").exists());
        assert!(!original.join("created").exists());
        assert!(!original.join("linked").exists());

        let _ = std::fs::remove_dir_all(root);
        let _ = std::fs::remove_dir_all(original);
    }
}
