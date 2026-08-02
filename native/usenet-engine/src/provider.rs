use crate::nntp;
use crate::reader_lease::random_lease_id;
use serde::Deserialize;
use std::collections::{HashMap, HashSet};
use std::sync::Arc;
use std::time::{Duration, Instant};

const MAX_PROVIDER_SETS: usize = 4096;
const PROVIDER_SET_IDLE_TTL: Duration = Duration::from_secs(15 * 60);

#[derive(Clone, Deserialize, Eq, PartialEq)]
pub struct Server {
    pub provider_configuration_id: String,
    pub connections: usize,
    pub pipeline: usize,
    pub priority: u16,
    #[serde(default)]
    pub backup: bool,
    #[serde(flatten)]
    pub request: nntp::BodyRequest,
}

#[derive(Deserialize)]
pub struct Registration {
    pub servers: Vec<Server>,
    pub account_partition: String,
}

pub struct ProviderSet {
    pub identity: String,
    pub generation: String,
    pub account_partition: [u8; 32],
    pub servers: Vec<Server>,
    pub pool_references: Vec<nntp::PoolReference>,
}

impl ProviderSet {
    pub fn diagnostics(&self) -> Vec<nntp::ProviderDiagnostics> {
        self.pool_references
            .iter()
            .map(nntp::PoolReference::diagnostics)
            .collect()
    }
}

struct Entry {
    provider_set: Arc<ProviderSet>,
    last_access: Instant,
}

pub struct Registry {
    entries: HashMap<String, Entry>,
    generations: HashMap<String, String>,
    pools: Arc<nntp::PoolRegistry>,
}

impl Registry {
    pub fn new(pools: Arc<nntp::PoolRegistry>) -> Self {
        Self {
            entries: HashMap::new(),
            generations: HashMap::new(),
            pools,
        }
    }

    pub fn register(
        &mut self,
        generation: &str,
        registration: Registration,
        now: Instant,
    ) -> Result<Arc<ProviderSet>, &'static str> {
        self.remove_expired(now);
        if !valid_generation(generation) {
            return Err("invalid_provider_set");
        }
        let account_partition = decode_partition(&registration.account_partition)?;
        let servers = normalize_servers(registration.servers)?;
        if let Some(identity) = self.generations.get(generation) {
            let entry = self
                .entries
                .get_mut(identity)
                .expect("provider generation index");
            if entry.provider_set.account_partition != account_partition
                || entry.provider_set.servers != servers
            {
                return Err("provider_set_conflict");
            }
            entry.last_access = now;
            return Ok(Arc::clone(&entry.provider_set));
        }
        if self.entries.len() >= MAX_PROVIDER_SETS {
            return Err("provider_set_capacity");
        }
        let identity = loop {
            let candidate = random_lease_id().map_err(|_| "provider_set_random_unavailable")?;
            if !self.entries.contains_key(&candidate) {
                break candidate;
            }
        };
        let pool_references = servers
            .iter()
            .map(|server| {
                self.pools.reference_for_generation(
                    &server.request,
                    server.connections,
                    server.pipeline,
                    generation,
                    server.priority,
                    server.backup,
                )
            })
            .collect::<Result<Vec<_>, _>>()?;
        let generation_key = generation.to_owned();
        let provider_set = Arc::new(ProviderSet {
            identity: identity.clone(),
            generation: generation_key.clone(),
            account_partition,
            servers,
            pool_references,
        });
        self.entries.insert(
            identity.clone(),
            Entry {
                provider_set: Arc::clone(&provider_set),
                last_access: now,
            },
        );
        self.generations.insert(generation_key, identity);
        Ok(provider_set)
    }

    pub fn acquire(
        &mut self,
        identity: &str,
        account_partition: [u8; 32],
        now: Instant,
    ) -> Result<Arc<ProviderSet>, &'static str> {
        self.remove_expired(now);
        let entry = self
            .entries
            .get_mut(identity)
            .ok_or("provider_set_unavailable")?;
        if entry.provider_set.account_partition != account_partition {
            return Err("provider_set_unavailable");
        }
        entry.last_access = now;
        Ok(Arc::clone(&entry.provider_set))
    }

    #[cfg(test)]
    pub fn remove(&mut self, identity: &str, now: Instant) -> Result<(), &'static str> {
        self.remove_expired(now);
        let entry = self
            .entries
            .get(identity)
            .ok_or("provider_set_unavailable")?;
        if Arc::strong_count(&entry.provider_set) != 1 {
            return Err("provider_set_busy");
        }
        let generation = entry.provider_set.generation.clone();
        self.entries.remove(identity);
        self.generations.remove(&generation);
        Ok(())
    }

    pub fn len(&mut self, now: Instant) -> usize {
        self.remove_expired(now);
        self.entries.len()
    }

    fn remove_expired(&mut self, now: Instant) {
        self.entries.retain(|_, entry| {
            Arc::strong_count(&entry.provider_set) != 1
                || now
                    .checked_duration_since(entry.last_access)
                    .is_none_or(|idle| idle < PROVIDER_SET_IDLE_TTL)
        });
        self.generations
            .retain(|_, identity| self.entries.contains_key(identity));
    }
}

pub fn valid_generation(value: &str) -> bool {
    valid_hex_partition(value)
}

pub fn valid_identity(value: &str) -> bool {
    value.len() == 22
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_'))
}

pub fn decode_partition(value: &str) -> Result<[u8; 32], &'static str> {
    if !valid_hex_partition(value) {
        return Err("invalid_provider_set");
    }
    let mut result = [0u8; 32];
    for (index, output) in result.iter_mut().enumerate() {
        *output = u8::from_str_radix(&value[index * 2..index * 2 + 2], 16)
            .expect("validated provider partition");
    }
    Ok(result)
}

fn valid_hex_partition(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

fn normalize_servers(mut servers: Vec<Server>) -> Result<Vec<Server>, &'static str> {
    if servers.is_empty() || servers.len() > 16 {
        return Err("invalid_provider_set");
    }
    let mut identifiers = HashSet::with_capacity(servers.len());
    for server in &servers {
        if server.provider_configuration_id.is_empty()
            || server.provider_configuration_id.len() > 128
            || server
                .provider_configuration_id
                .bytes()
                .any(|byte| byte.is_ascii_control() || byte == b' ')
            || !identifiers.insert(server.provider_configuration_id.as_str())
            || !(1..=100).contains(&server.connections)
            || !(1..=16).contains(&server.pipeline)
            || server.priority > 1000
            || !server.request.message_id.is_empty()
            || nntp::validate_body_template(&server.request).is_err()
        {
            return Err("invalid_provider_set");
        }
    }
    // This stable sort retains configured list order as the tie-breaker inside
    // one primary/backup priority class.
    servers.sort_by_key(|server| (server.backup, server.priority));
    Ok(servers)
}

#[cfg(test)]
mod tests {
    use super::{PROVIDER_SET_IDLE_TTL, Registration, Registry, Server, valid_identity};
    use crate::nntp::BodyRequest;
    use std::sync::Arc;
    use std::time::{Duration, Instant};

    fn server(identifier: &str, priority: u16, backup: bool) -> Server {
        Server {
            provider_configuration_id: identifier.into(),
            connections: 4,
            pipeline: 2,
            priority,
            backup,
            request: BodyRequest {
                host: "news.example.test".into(),
                port: 563,
                tls_mode: "implicit".into(),
                allow_private: false,
                username: Some("user".into()),
                password: Some("secret".into()),
                message_id: String::new(),
            },
        }
    }

    fn registration(servers: Vec<Server>) -> Registration {
        Registration {
            servers,
            account_partition: "a".repeat(64),
        }
    }

    fn registry() -> Registry {
        Registry::new(Arc::new(
            crate::nntp::PoolRegistry::new(100).expect("create provider test pools"),
        ))
    }

    #[test]
    fn registration_is_idempotent_normalized_and_partition_bound() {
        let now = Instant::now();
        let mut registry = registry();
        let first = registry
            .register(
                &"b".repeat(64),
                registration(vec![
                    server("backup", 0, true),
                    server("primary", 10, false),
                ]),
                now,
            )
            .expect("register provider set");
        assert_eq!(first.servers[0].provider_configuration_id, "primary");
        let second = registry
            .register(
                &"b".repeat(64),
                registration(vec![
                    server("primary", 10, false),
                    server("backup", 0, true),
                ]),
                now,
            )
            .expect("repeat equivalent provider set");
        assert!(Arc::ptr_eq(&first, &second));
        assert!(valid_identity(&first.identity));
        assert_ne!(first.identity, "b".repeat(64));
        assert!(registry.acquire(&first.identity, [0xaa; 32], now).is_ok());
        assert!(matches!(
            registry.acquire(&first.identity, [0xbb; 32], now),
            Err("provider_set_unavailable")
        ));
    }

    #[test]
    fn public_identity_changes_when_the_registry_is_recreated() {
        let now = Instant::now();
        let generation = "b".repeat(64);
        let first = registry()
            .register(
                &generation,
                registration(vec![server("primary", 0, false)]),
                now,
            )
            .expect("register first runtime provider set");
        let second = registry()
            .register(
                &generation,
                registration(vec![server("primary", 0, false)]),
                now,
            )
            .expect("register recreated runtime provider set");

        assert_eq!(first.generation, second.generation);
        assert!(valid_identity(&first.identity));
        assert!(valid_identity(&second.identity));
        assert_ne!(first.identity, second.identity);
    }

    #[test]
    fn conflicting_or_duplicate_registration_fails_closed() {
        let now = Instant::now();
        let mut registry = registry();
        registry
            .register(
                &"b".repeat(64),
                registration(vec![server("primary", 0, false)]),
                now,
            )
            .expect("register provider set");
        assert!(matches!(
            registry.register(
                &"b".repeat(64),
                registration(vec![server("changed", 0, false)]),
                now,
            ),
            Err("provider_set_conflict")
        ));
        assert!(matches!(
            registry.register(
                &"c".repeat(64),
                registration(vec![
                    server("duplicate", 0, false),
                    server("duplicate", 1, false),
                ]),
                now,
            ),
            Err("invalid_provider_set")
        ));
        let mut explicitly_plaintext = server("plaintext-auth", 0, false);
        explicitly_plaintext.request.tls_mode = "plaintext".into();
        let registered = registry
            .register(
                &"d".repeat(64),
                registration(vec![explicitly_plaintext]),
                now,
            )
            .expect("register explicitly selected plaintext authentication");
        assert_eq!(
            registered.servers[0].request.username.as_deref(),
            Some("user")
        );
    }

    #[test]
    fn equal_priority_order_is_preserved_and_generation_significant() {
        let now = Instant::now();
        let mut registry = registry();
        let generation = "d".repeat(64);
        let first = registry
            .register(
                &generation,
                registration(vec![
                    server("listed-first", 10, false),
                    server("listed-second", 10, false),
                ]),
                now,
            )
            .expect("register ordered provider set");
        assert_eq!(
            first
                .servers
                .iter()
                .map(|server| server.provider_configuration_id.as_str())
                .collect::<Vec<_>>(),
            ["listed-first", "listed-second"]
        );
        assert!(matches!(
            registry.register(
                &generation,
                registration(vec![
                    server("listed-second", 10, false),
                    server("listed-first", 10, false),
                ]),
                now,
            ),
            Err("provider_set_conflict")
        ));
    }

    #[test]
    fn live_references_block_delete_and_idle_expiry() {
        let now = Instant::now();
        let mut registry = registry();
        let reference = registry
            .register(
                &"b".repeat(64),
                registration(vec![server("primary", 0, false)]),
                now,
            )
            .expect("register referenced provider set");
        assert_eq!(
            registry.remove(&reference.identity, now),
            Err("provider_set_busy")
        );
        assert_eq!(
            registry.len(now + PROVIDER_SET_IDLE_TTL + Duration::from_secs(1)),
            1
        );
        drop(reference);
        assert_eq!(
            registry.len(now + PROVIDER_SET_IDLE_TTL + Duration::from_secs(1)),
            0
        );
    }
}
