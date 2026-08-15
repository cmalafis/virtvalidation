// Coerce an API result into an array.
//
// `x ?? []` is not enough. It guards null and undefined but not a
// wrong-typed value, and these endpoints return several shapes:
//
//   * a bare list                       -> use it
//   * {items, total, skip, limit}       -> use .items
//   * {} or an error-shaped object      -> use []
//
// Pages that do `(data ?? []).map(...)` blow up on the third case. Use
// this wherever a fetch result becomes list state.

export function asArray(value) {
  if (Array.isArray(value)) return value;
  if (Array.isArray(value?.items)) return value.items;
  return [];
}
