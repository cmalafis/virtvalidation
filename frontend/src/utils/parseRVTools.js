// List-cell splitting for the manual Add-VM form.
//
// This file used to hold a browser-side RVTools/CSV parser (SheetJS). File
// imports are parsed on the server now — see POST /api/imports/rvtools and
// backend/app/core/rvtools_parser.py — so the only thing left here is the
// helper the manual form shares with the importer's list convention:
// multiple networks/datastores in one field, separated by ";" or ",".
export const splitList = (raw) => {
  if (!raw) return [];
  return String(raw)
    .split(/[;,]/)
    .map((s) => s.trim())
    .filter((s) => s.length > 0);
};
