#!/usr/bin/env node

import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const projectRoot = path.resolve(here, "..");
const defaultSource = path.resolve(
  projectRoot,
  "../audit-travel/tour-city-planner/app.js",
);
const sourcePath = path.resolve(process.argv[2] || defaultSource);
const outputPath = path.join(projectRoot, "data/destinations.json");

if (!fs.existsSync(sourcePath)) {
  throw new Error(`Legacy app.js not found: ${sourcePath}`);
}

const source = fs.readFileSync(sourcePath, "utf8");
const dataEnd = source.indexOf("const DEFAULT_DESTINATION_ID");
if (dataEnd < 0) throw new Error("Could not locate DESTINATIONS boundary");

const context = {};
vm.createContext(context);
vm.runInContext(
  `${source.slice(0, dataEnd)}\nthis.__destinations = DESTINATIONS;` +
    "this.__countryRegions = COUNTRY_REGIONS;" +
    "this.__icons = ACTIVITY_ICON_OPTIONS;",
  context,
);

const labelsStart = source.indexOf("const LOCATION_LABELS =");
const labelsEnd = source.indexOf("const DEFAULT_BASE_AMOUNTS", labelsStart);
if (labelsStart < 0 || labelsEnd < 0) {
  throw new Error("Could not locate LOCATION_LABELS");
}
vm.runInContext(
  `${source.slice(labelsStart, labelsEnd)}\nthis.__labels = LOCATION_LABELS;`,
  context,
);

const labels = context.__labels;
const localizeCity = (destination) => {
  const specialKey = `${destination.city}_city`;
  return labels[specialKey] || labels[destination.city] || destination.city;
};

const destinations = Object.fromEntries(
  Object.entries(context.__destinations).map(([id, destination]) => [
    id,
    {
      ...destination,
      cityKo: localizeCity(destination),
      countryKo: labels[destination.country] || destination.country,
      region: context.__countryRegions[destination.country] || "other",
    },
  ]),
);

const catalog = {
  schemaVersion: 1,
  source: {
    repository: "https://github.com/minwoo19930301/tour-city-planner",
    commit: "c0e544f604adfdc52d5c2c93480c5ac4d0aeea7e",
    license: "CC BY-NC 4.0 with the original commercial exception notice",
  },
  destinationCount: Object.keys(destinations).length,
  allowedIcons: context.__icons.map(({ value }) => value),
  destinations,
};

fs.mkdirSync(path.dirname(outputPath), { recursive: true });
fs.writeFileSync(outputPath, `${JSON.stringify(catalog, null, 2)}\n`, "utf8");
console.log(`Wrote ${catalog.destinationCount} destinations to ${outputPath}`);
