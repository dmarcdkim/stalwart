/*
 * SPDX-FileCopyrightText: 2020 Stalwart Labs LLC <hello@stalw.art>
 *
 * SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-SEL
 */

//! Consistency of the generated `resources/schema/` pair with this crate's enums.
//!
//! `schema.json.gz` is served verbatim by `GET /api/schema` and has no generator
//! in this tree, so nothing else notices when it drifts away from the Rust
//! definitions. Two properties are load-bearing:
//!
//!  * `schema.json.sha256` is the unpadded base64url SHA-256 of the GZIP BYTES and
//!    is `include_str!`d straight into the `/api/schema/<hash>` cache URL, which is
//!    served immutable, so a stale value or a stray newline pins clients to the
//!    wrong document under that key;
//!  * the blob lists each enum in DECLARATION order. Values cross the API as name
//!    strings (`Serialize for ExpressionConstant` is `serialize_str(as_str())`), so
//!    an index is not a wire value. But for enums whose declaration order matches
//!    their discriminant order -- `ExpressionConstant` is one -- index == `from_id`
//!    is an exact and very cheap detector for a stale or hand-edited blob.
//!
//! Regenerate with `./resources/schema/regenerate.py` after touching either side.

use base64::{Engine, engine::general_purpose::URL_SAFE_NO_PAD};
use flate2::read::GzDecoder;
use registry::{
    schema::enums::{ExpressionConstant, MTA_AGGREGATE_CONSTANT},
    types::EnumImpl,
};
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::io::Read;

static SCHEMA_GZ: &[u8] = include_bytes!("../../../resources/schema/schema.json.gz");
const SCHEMA_HASH: &str = include_str!("../../../resources/schema/schema.json.sha256");

fn schema() -> Value {
    let mut json = String::new();
    GzDecoder::new(SCHEMA_GZ)
        .read_to_string(&mut json)
        .expect("schema.json.gz is not valid gzip");
    serde_json::from_str(&json).expect("schema.json.gz does not contain valid JSON")
}

fn enum_names(name: &str) -> Vec<String> {
    schema()["enums"][name]
        .as_array()
        .unwrap_or_else(|| panic!("schema has no enums.{name}"))
        .iter()
        .map(|entry| {
            entry["name"]
                .as_str()
                .unwrap_or_else(|| panic!("enums.{name} entry has no string `name`"))
                .to_string()
        })
        .collect()
}

#[test]
fn schema_hash_matches_blob() {
    assert_eq!(
        SCHEMA_HASH,
        SCHEMA_HASH.trim(),
        "schema.json.sha256 must have no surrounding whitespace: it is inlined into \
         the /api/schema/<hash> URL verbatim"
    );
    assert_eq!(
        SCHEMA_HASH,
        URL_SAFE_NO_PAD.encode(Sha256::digest(SCHEMA_GZ)),
        "schema.json.sha256 is stale; run ./resources/schema/regenerate.py"
    );
}

#[test]
fn expression_constant_ids_are_dense() {
    for id in 0..ExpressionConstant::COUNT {
        let variant = ExpressionConstant::from_id(id as u16)
            .unwrap_or_else(|| panic!("ExpressionConstant::from_id({id}) is None"));
        assert_eq!(
            variant.to_id() as usize,
            id,
            "{variant:?} has a mismatched id"
        );
        assert_eq!(
            ExpressionConstant::parse(variant.as_str()),
            Some(variant),
            "{variant:?} does not survive as_str() -> parse()"
        );
    }
    assert!(
        ExpressionConstant::from_id(ExpressionConstant::COUNT as u16).is_none(),
        "ExpressionConstant::COUNT is lower than the number of variants"
    );
}

#[test]
fn expression_constant_matches_schema() {
    let names = enum_names("ExpressionConstant");
    assert_eq!(
        names.len(),
        ExpressionConstant::COUNT,
        "enums.ExpressionConstant has {} entries, COUNT is {}; run \
         ./resources/schema/regenerate.py",
        names.len(),
        ExpressionConstant::COUNT
    );
    for (id, name) in names.iter().enumerate() {
        let variant = ExpressionConstant::from_id(id as u16)
            .unwrap_or_else(|| panic!("schema lists {name:?} at {id} with no variant"));
        assert_eq!(
            variant.as_str(),
            name,
            "enums.ExpressionConstant[{id}] is {name:?} but discriminant {id} is \
             {variant:?}; the blob is stale, or an entry was inserted rather than \
             appended. Run ./resources/schema/regenerate.py"
        );
    }
}

#[test]
fn minutely_aggregate_frequency_is_wired_up() {
    let minutely = ExpressionConstant::parse("minutely").expect("no `minutely` constant");
    assert!(
        MTA_AGGREGATE_CONSTANT.contains(&minutely),
        "`minutely` is not offered as an aggregate report frequency"
    );
    assert_eq!(
        enum_names("ExpressionConstant").get(minutely.to_id() as usize),
        Some(&"minutely".to_string()),
        "the schema blob does not carry `minutely` at its discriminant; run \
         ./resources/schema/regenerate.py"
    );
}
