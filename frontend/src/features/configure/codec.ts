import { deflateSync, inflateSync } from "fflate";
import { z } from "zod";
import { CONFIGURATION_DICTIONARY_V1, type ConfigModel } from "../../api/generated/contracts";

const MAX_JSON_BYTES = 24 * 1024;
const MAX_ENCODED_BYTES = 32 * 1024;
const COMPRESSED_PREFIX = "z1.";
const COMPRESSION_DICTIONARY = new TextEncoder().encode(CONFIGURATION_DICTIONARY_V1);
const configurationDocument = z.looseObject({
  schemaVersion: z.union([z.literal(1), z.literal(2)]).optional(),
});

export interface EncodedConfiguration {
  encoded: string;
  warnAboutRequestLine: boolean;
}

type SerializableConfiguration = ConfigModel;

function encodeBase64Url(bytes: Uint8Array): string {
  let binary = "";
  for (let offset = 0; offset < bytes.length; offset += 32_768) {
    binary += String.fromCharCode(...bytes.subarray(offset, offset + 32_768));
  }
  return btoa(binary).replaceAll("+", "-").replaceAll("/", "_").replace(/=+$/, "");
}

function decodeBase64(value: string): Uint8Array {
  const standard = value.replaceAll("-", "+").replaceAll("_", "/");
  const binary = atob(standard + "=".repeat((4 - (standard.length % 4)) % 4));
  return Uint8Array.from(binary, (character) => character.charCodeAt(0));
}

export function encodeConfiguration(
  configuration: SerializableConfiguration,
): EncodedConfiguration {
  const bytes = new TextEncoder().encode(JSON.stringify(configuration));
  if (bytes.length > MAX_JSON_BYTES) {
    throw new Error("configuration_too_large");
  }
  const legacy = encodeBase64Url(bytes);
  const compressed = `${COMPRESSED_PREFIX}${encodeBase64Url(
    deflateSync(bytes, { dictionary: COMPRESSION_DICTIONARY, level: 9 }),
  )}`;
  const encoded = compressed.length < legacy.length ? compressed : legacy;
  if (encoded.length > MAX_ENCODED_BYTES) {
    throw new Error("configuration_too_large");
  }
  return { encoded, warnAboutRequestLine: encoded.length > 8 * 1024 };
}

export function decodeConfiguration(value: string): ConfigModel {
  if (value.length > MAX_ENCODED_BYTES) {
    throw new Error("configuration_too_large");
  }
  const bytes = value.startsWith(COMPRESSED_PREFIX)
    ? inflateSync(decodeBase64(value.slice(COMPRESSED_PREFIX.length)), {
        dictionary: COMPRESSION_DICTIONARY,
        out: new Uint8Array(MAX_JSON_BYTES + 1),
      })
    : decodeBase64(value);
  if (bytes.length > MAX_JSON_BYTES) {
    throw new Error("configuration_too_large");
  }
  const document: unknown = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes));
  return configurationDocument.parse(document) as ConfigModel;
}

export function manifestLocation(
  configuration: SerializableConfiguration | undefined,
  apiPrefix: string,
  install: boolean,
): { url: string; warnAboutRequestLine: boolean } {
  const encoded = configuration ? encodeConfiguration(configuration) : null;
  const path = encoded
    ? `${apiPrefix}/${encoded.encoded}/manifest.json`
    : `${apiPrefix}/manifest.json`;
  return {
    url: install ? `stremio://${window.location.host}${path}` : `${window.location.origin}${path}`,
    warnAboutRequestLine: encoded?.warnAboutRequestLine ?? false,
  };
}
