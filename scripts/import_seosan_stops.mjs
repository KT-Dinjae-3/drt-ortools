#!/usr/bin/env node

/**
 * Build data/seosan_stops.json from the reviewed TSV stop registry.
 *
 * The TSV contains the canonical stop IDs and review metadata.  Its three
 * public Kakao Map folder links contain the physical marker coordinates.  We
 * join them by the original stop name and convert Kakao's WCONGNAMUL marker
 * coordinates to WGS84 with Kakao's pinned public conversion library.
 *
 * Usage:
 *   node scripts/import_seosan_stops.mjs [INPUT.tsv] [OUTPUT.json]
 */

import { createHash } from "node:crypto";
import { readFileSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { mkdirSync } from "node:fs";
import vm from "node:vm";

const [, , inputArg = "data/seosan_stops_source.tsv", outputArg = "data/seosan_stops.json"] =
  process.argv;

const INPUT_PATH = resolve(inputArg);
const OUTPUT_PATH = resolve(outputArg);
const SOURCE_SHA256 = createHash("sha256")
  .update(readFileSync(INPUT_PATH))
  .digest("hex");

const GROUPS = {
  "대산": {
    region_code: "SEOSAN_DAESAN",
    folder_id: 17248092,
    source_url: "https://kko.to/W3JuXfEktk",
  },
  "해미": {
    region_code: "SEOSAN_HAEMI",
    folder_id: 17275726,
    source_url: "https://kko.to/RQxo72diCp",
  },
  "고북": {
    region_code: "SEOSAN_GOBUK",
    folder_id: 17260132,
    source_url: "https://kko.to/3j76rSlFKa",
  },
};

const CONVERTER_URL =
  "https://t1.kakaocdn.net/mapjsapi/js/libs/congnamul.js";
const CONVERTER_SHA256 =
  "b2210e3aacdad496427d70179118f6df3be946477eaca2f4bc58f8a41eca2e61";

function parseTsv(path) {
  const text = readFileSync(path, "utf8")
    .replace(/^\uFEFF/, "")
    .replace(/\r\n/g, "\n")
    .replace(/\r/g, "\n");
  const lines = text.split("\n").filter((line) => line.length > 0);
  const headers = lines[0].split("\t");
  if (headers.length !== 13) {
    throw new Error(`expected 13 TSV columns, got ${headers.length}`);
  }

  return lines.slice(1).map((line, index) => {
    const values = line.split("\t");
    if (values.length !== headers.length) {
      throw new Error(
        `line ${index + 2}: expected ${headers.length} columns, got ${values.length}`,
      );
    }
    return Object.fromEntries(headers.map((header, i) => [header, values[i]]));
  });
}

async function loadCoordinateConverter() {
  const response = await fetch(CONVERTER_URL);
  if (!response.ok) {
    throw new Error(`coordinate converter download failed: ${response.status}`);
  }
  const source = await response.text();
  const digest = createHash("sha256").update(source).digest("hex");
  if (digest !== CONVERTER_SHA256) {
    throw new Error(
      `coordinate converter checksum changed: expected ${CONVERTER_SHA256}, got ${digest}`,
    );
  }

  globalThis.daum = {
    maps: {
      LatLng: class LatLng {
        constructor(lat, lng) {
          this.lat = lat;
          this.lng = lng;
        }
      },
    },
  };
  vm.runInThisContext(source, { filename: "congnamul.js" });

  return (x, y) => {
    const converted = new daum.maps.Congnamul(Number(x), Number(y)).toLatLng();
    if (!Number.isFinite(converted.lat) || !Number.isFinite(converted.lng)) {
      throw new Error(`invalid converted coordinate for (${x}, ${y})`);
    }
    return {
      lat: Number(converted.lat.toFixed(8)),
      lng: Number(converted.lng.toFixed(8)),
    };
  };
}

async function loadPublicFolder(regionName, group) {
  const url = `https://map.kakao.com/favorite/list?folderid=${group.folder_id}`;
  const response = await fetch(url, {
    headers: {
      Referer: `https://map.kakao.com/?target=other&folderid=${group.folder_id}`,
      "User-Agent": "Mozilla/5.0",
      "X-Requested-With": "XMLHttpRequest",
    },
  });
  if (!response.ok) {
    throw new Error(`${regionName} public folder failed: ${response.status}`);
  }
  const body = await response.json();
  if (!Array.isArray(body.favorites)) {
    throw new Error(`${regionName} public folder did not return favorites`);
  }

  const byName = new Map();
  for (const item of body.favorites) {
    if (byName.has(item.display1)) {
      throw new Error(`${regionName}: duplicate map marker name ${item.display1}`);
    }
    if (!Number.isFinite(Number(item.x)) || !Number.isFinite(Number(item.y))) {
      throw new Error(`${regionName}: marker ${item.display1} has no coordinates`);
    }
    byName.set(item.display1, item);
  }
  return byName;
}

function uniqueNonEmpty(values) {
  return [...new Set(values.map((value) => value.trim()).filter(Boolean))];
}

const rows = parseTsv(INPUT_PATH);
const ids = new Set();
for (const row of rows) {
  const stopId = row["통합ID"];
  if (ids.has(stopId)) {
    throw new Error(`duplicate stop ID: ${stopId}`);
  }
  ids.add(stopId);
  if (!GROUPS[row["권역"]]) {
    throw new Error(`unknown region: ${row["권역"]}`);
  }
}

const convert = await loadCoordinateConverter();
const markerIndexes = Object.fromEntries(
  await Promise.all(
    Object.entries(GROUPS).map(async ([name, group]) => [
      name,
      await loadPublicFolder(name, group),
    ]),
  ),
);

const stops = rows.map((row) => {
  const regionName = row["권역"];
  const group = GROUPS[regionName];
  const sourceName = row["정류장명(원본명 기준)"];
  const originalAliases = uniqueNonEmpty([row["원본 별칭"]]);
  const matchNames = uniqueNonEmpty([sourceName, ...originalAliases]);
  const marker = matchNames
    .map((name) => markerIndexes[regionName].get(name))
    .find(Boolean);
  if (!marker) {
    throw new Error(
      `${row["통합ID"]}: no Kakao marker for ${matchNames.join(" / ")}`,
    );
  }
  markerIndexes[regionName].delete(marker.display1);

  const coordinate = convert(marker.x, marker.y);
  const displayName = row["대표명(끝 번호 제거)"];
  const aliases = uniqueNonEmpty([
    sourceName,
    displayName,
    ...originalAliases,
  ]);

  return {
    stop_id: row["통합ID"],
    region_code: group.region_code,
    region_name: regionName,
    source_name: sourceName,
    display_name: displayName,
    source_item_count: Number(row["원본 항목 수"]),
    source_type: row["유형"],
    address: row["주소"] || null,
    has_address: row["주소 여부"] === "있음",
    kakao_group: row["카카오맵 그룹"],
    source_url: row["원본 URL"],
    duplicate_review_id:
      row["중복 검토ID"] === "-" ? null : row["중복 검토ID"],
    review_status: row["검토 상태"],
    aliases,
    map_marker: {
      seq: marker.seq,
      type: marker.type,
      name: marker.display1,
      address: marker.display2 || marker.memo || null,
      x: Number(marker.x),
      y: Number(marker.y),
    },
    lat: coordinate.lat,
    lng: coordinate.lng,
  };
});

for (const [regionName, remaining] of Object.entries(markerIndexes)) {
  if (remaining.size > 0) {
    throw new Error(
      `${regionName}: ${remaining.size} public markers were not matched: ${[
        ...remaining.keys(),
      ]
        .slice(0, 10)
        .join(", ")}`,
    );
  }
}

const regionCounts = Object.fromEntries(
  Object.keys(GROUPS).map((regionName) => [
    GROUPS[regionName].region_code,
    stops.filter((stop) => stop.region_name === regionName).length,
  ]),
);

const output = {
  metadata: {
    schema_version: 1,
    generated_at: new Date().toISOString(),
    source_file: "data/seosan_stops_source.tsv",
    source_sha256: SOURCE_SHA256,
    source_rows: stops.length,
    region_counts: regionCounts,
    coordinate_source: "Kakao Map public folders (WCONGNAMUL -> WGS84)",
    coordinate_converter: {
      url: CONVERTER_URL,
      sha256: CONVERTER_SHA256,
    },
    public_folders: Object.fromEntries(
      Object.entries(GROUPS).map(([name, group]) => [name, group]),
    ),
  },
  stops,
};

mkdirSync(dirname(OUTPUT_PATH), { recursive: true });
writeFileSync(OUTPUT_PATH, `${JSON.stringify(output, null, 2)}\n`, "utf8");
console.log(
  `wrote ${stops.length} stops to ${OUTPUT_PATH}: ${JSON.stringify(regionCounts)}`,
);
