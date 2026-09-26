//! Schema model and evolution rules.
//!
//! A [`Schema`] describes one message type: an ordered set of named
//! [`FieldDef`]s, each with a field number, a scalar/message [`TypeDef`]
//! and a [`Cardinality`]. Schemas are built from the JSON control surface
//! (see `jsonio`) but have no dependency on JSON internally.

use std::collections::BTreeMap;

use crate::error::{Error, Result};
use crate::wire::WireType;

/// Scalar value kinds.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum ScalarType {
    /// 32-bit signed integer, zig-zag varint on the wire.
    Int32,
    /// 64-bit signed integer, zig-zag varint on the wire.
    Int64,
    /// 32-bit unsigned integer, plain varint on the wire.
    Uint32,
    /// 64-bit unsigned integer, plain varint on the wire.
    Uint64,
    /// IEEE-754 single precision, wire type I32.
    Float,
    /// IEEE-754 double precision, wire type I64.
    Double,
    /// UTF-8 text, wire type LEN.
    String,
    /// Opaque bytes, wire type LEN.
    Bytes,
    /// Boolean, varint restricted to 0/1.
    Bool,
}

impl ScalarType {
    /// Parse the canonical JSON name used in schema documents.
    pub fn from_name(name: &str) -> Result<ScalarType> {
        Ok(match name {
            "int32" => ScalarType::Int32,
            "int64" => ScalarType::Int64,
            "uint32" => ScalarType::Uint32,
            "uint64" => ScalarType::Uint64,
            "float" => ScalarType::Float,
            "double" => ScalarType::Double,
            "string" => ScalarType::String,
            "bytes" => ScalarType::Bytes,
            "bool" => ScalarType::Bool,
            other => return Err(Error::Schema(format!("unknown scalar type '{other}'"))),
        })
    }

    /// Canonical JSON name.
    pub fn as_name(self) -> &'static str {
        match self {
            ScalarType::Int32 => "int32",
            ScalarType::Int64 => "int64",
            ScalarType::Uint32 => "uint32",
            ScalarType::Uint64 => "uint64",
            ScalarType::Float => "float",
            ScalarType::Double => "double",
            ScalarType::String => "string",
            ScalarType::Bytes => "bytes",
            ScalarType::Bool => "bool",
        }
    }

    /// Wire type used for a single (non-packed) occurrence.
    pub fn wire_type(self) -> WireType {
        match self {
            ScalarType::Int32
            | ScalarType::Int64
            | ScalarType::Uint32
            | ScalarType::Uint64
            | ScalarType::Bool => WireType::Varint,
            ScalarType::Float => WireType::I32,
            ScalarType::Double => WireType::I64,
            ScalarType::String | ScalarType::Bytes => WireType::Len,
        }
    }
}

/// A field's type: scalar or a reference to another named message.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub enum TypeDef {
    Scalar(ScalarType),
    /// Name of another message type in the same schema bundle.
    Message(String),
}

/// How many times a field may occur and whether it may be absent.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Cardinality {
    /// Exactly one value; absence on read is an error.
    Required,
    /// Zero or one value. Absence is distinct from an explicit value,
    /// including an explicit zero.
    Optional,
    /// Zero to many values.
    Repeated,
}

impl Cardinality {
    /// Parse the canonical JSON name (`singular` is accepted as a synonym
    /// for `required` to match familiar terminology).
    pub fn from_name(name: &str) -> Result<Cardinality> {
        Ok(match name {
            "required" | "singular" => Cardinality::Required,
            "optional" => Cardinality::Optional,
            "repeated" => Cardinality::Repeated,
            other => return Err(Error::Schema(format!("unknown cardinality '{other}'"))),
        })
    }

    /// Canonical JSON name.
    pub fn as_name(self) -> &'static str {
        match self {
            Cardinality::Required => "required",
            Cardinality::Optional => "optional",
            Cardinality::Repeated => "repeated",
        }
    }
}

/// One field definition.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FieldDef {
    pub name: String,
    pub number: u32,
    pub ty: TypeDef,
    pub cardinality: Cardinality,
    /// When true, a `repeated` scalar is encoded packed into one LEN field.
    pub packed: bool,
}

impl FieldDef {
    /// Build a field with defaults appropriate to its cardinality
    /// (`repeated` scalar fields default to packed, matching the format
    /// specification).
    pub fn new(
        name: impl Into<String>,
        number: u32,
        ty: TypeDef,
        cardinality: Cardinality,
    ) -> FieldDef {
        let packed = cardinality == Cardinality::Repeated
            && matches!(ty, TypeDef::Scalar(_))
            && !matches!(ty, TypeDef::Scalar(ScalarType::String | ScalarType::Bytes));
        FieldDef {
            name: name.into(),
            number,
            ty,
            cardinality,
            packed,
        }
    }
}

/// One message type definition: a name plus its fields.
#[derive(Debug, Clone)]
pub struct MessageDef {
    pub name: String,
    pub fields: BTreeMap<u32, FieldDef>,
}

impl MessageDef {
    /// Look a field up by number.
    pub fn field(&self, number: u32) -> Option<&FieldDef> {
        self.fields.get(&number)
    }
}

/// A bundle of named message types. Every message encoded or decoded under
/// the schema names its root type; embedded references resolve inside the
/// bundle.
#[derive(Debug, Clone)]
pub struct Schema {
    pub messages: BTreeMap<String, MessageDef>,
    pub root: String,
}

impl Schema {
    /// Construct and validate a schema bundle.
    pub fn new(messages: BTreeMap<String, MessageDef>, root: impl Into<String>) -> Result<Schema> {
        let root = root.into();
        if !messages.contains_key(&root) {
            return Err(Error::Schema(format!(
                "root message type '{root}' is not defined"
            )));
        }
        let schema = Schema { messages, root };
        schema.validate()?;
        Ok(schema)
    }

    /// The root message definition.
    pub fn root_message(&self) -> &MessageDef {
        &self.messages[&self.root]
    }

    /// Look a named message up.
    pub fn message(&self, name: &str) -> Option<&MessageDef> {
        self.messages.get(name)
    }

    fn validate(&self) -> Result<()> {
        for (msg_name, msg) in &self.messages {
            let mut by_name: BTreeMap<&str, u32> = BTreeMap::new();
            for (num, field) in &msg.fields {
                if *num == 0 {
                    return Err(Error::Schema(format!(
                        "field '{}.{}' uses reserved number 0",
                        msg_name, field.name
                    )));
                }
                if let Some(prev) = by_name.insert(field.name.as_str(), *num) {
                    return Err(Error::Schema(format!(
                        "duplicate field name '{}' in message '{msg_name}' (numbers {prev}, {num})",
                        field.name
                    )));
                }
                if field.cardinality == Cardinality::Repeated
                    && field.packed
                    && matches!(field.ty, TypeDef::Message(_))
                {
                    return Err(Error::Schema(format!(
                        "packed repeated field '{}' in '{msg_name}' must be scalar",
                        field.name
                    )));
                }
                if let TypeDef::Message(target) = &field.ty {
                    if !self.messages.contains_key(target) {
                        return Err(Error::Schema(format!(
                            "field '{}.{}' references undefined message type '{target}'",
                            msg_name, field.name
                        )));
                    }
                }
            }
        }
        Ok(())
    }
}

// ---------------------------------------------------------------------------
// Evolution compatibility
// ---------------------------------------------------------------------------

/// Outcome of comparing two versions of the same field.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum EvolutionVerdict {
    /// Wire-compatible in both directions.
    Compatible,
    /// Readable in one direction only; [`check_evolution`] reports which.
    Directional(String),
    /// Rejected: types or cardinality changed incompatibly.
    Incompatible(String),
}

fn wire_compatible_scalar(old: ScalarType, new: ScalarType) -> EvolutionVerdict {
    use ScalarType::*;
    if old == new {
        return EvolutionVerdict::Compatible;
    }
    // The same wire family is always safe across varint width / sign.
    let family = |t: ScalarType| match t {
        Int32 | Int64 | Uint32 | Uint64 | Bool => 0u8,
        Float => 1,
        Double => 2,
        String | Bytes => 3,
    };
    if family(old) == family(new) {
        // bool<->int is accepted on the wire but semantically lossy:
        // callers may rely on 0/1; mark directional rather than silent.
        if matches!((old, new), (Bool, _) | (_, Bool))
            && !(matches!(old, Bool) && matches!(new, Bool))
        {
            return EvolutionVerdict::Directional(format!(
                "{} -> {} shares the varint family but bool values are only 0/1",
                old.as_name(),
                new.as_name()
            ));
        }
        return EvolutionVerdict::Compatible;
    }
    EvolutionVerdict::Incompatible(format!(
        "wire type changes from {} ({}) to {} ({}): the byte layout cannot be read",
        old.as_name(),
        old.wire_type() as u8,
        new.as_name(),
        new.wire_type() as u8
    ))
}

fn type_compatible(old: &TypeDef, new: &TypeDef) -> EvolutionVerdict {
    match (old, new) {
        (TypeDef::Scalar(a), TypeDef::Scalar(b)) => wire_compatible_scalar(*a, *b),
        (TypeDef::Message(a), TypeDef::Message(b)) if a == b => EvolutionVerdict::Compatible,
        (TypeDef::Message(a), TypeDef::Message(b)) => {
            EvolutionVerdict::Incompatible(format!("embedded message type changed '{a}' -> '{b}'"))
        }
        _ => EvolutionVerdict::Incompatible(
            "scalar and message types are not interchangeable".into(),
        ),
    }
}

fn cardinality_compatible(old: Cardinality, new: Cardinality) -> EvolutionVerdict {
    use Cardinality::*;
    match (old, new) {
        (Required, Required) | (Optional, Optional) | (Repeated, Repeated) => {
            EvolutionVerdict::Compatible
        }
        // Widening: older single values read fine as the first element;
        // an optional that is present reads the same.
        (Required, Optional) | (Optional, Repeated) | (Required, Repeated) => {
            EvolutionVerdict::Compatible
        }
        // Narrowing can only work if runtime data happens to cooperate;
        // reject it at the schema-compatibility layer.
        (Optional, Required) => EvolutionVerdict::Incompatible(
            "optional -> required: old data may omit the field".into(),
        ),
        (Repeated, Required) => EvolutionVerdict::Incompatible(
            "repeated -> required: old data may carry zero or many values".into(),
        ),
        (Repeated, Optional) => EvolutionVerdict::Incompatible(
            "repeated -> optional: old data may carry many values".into(),
        ),
    }
}

/// Compare field `number` across two schema versions of a message.
///
/// `None` means the field does not exist in that version. A field present
/// in only one side is always compatible (added fields must be optional/
/// repeated in the new version; removed fields become unknown fields and
/// are preserved by the decoder).
pub fn field_evolution(
    _number: u32,
    old: Option<&FieldDef>,
    new: Option<&FieldDef>,
) -> EvolutionVerdict {
    match (old, new) {
        (None, Some(f)) => {
            if f.cardinality == Cardinality::Required {
                EvolutionVerdict::Incompatible(
                    "newly added field is 'required': old data cannot contain it".into(),
                )
            } else {
                EvolutionVerdict::Compatible
            }
        }
        (Some(_), None) => EvolutionVerdict::Compatible, // removed -> unknown, preserved
        (Some(a), Some(b)) => {
            let ty = type_compatible(&a.ty, &b.ty);
            let card = cardinality_compatible(a.cardinality, b.cardinality);
            // Packed <-> unpacked for repeated scalars is transparently
            // accepted by the decoder (a single packed LEN and multiple
            // scalar tags both decode into the same list); only flag it
            // when cardinality changed away from repeated.
            match (ty, card) {
                (EvolutionVerdict::Incompatible(r), _) => EvolutionVerdict::Incompatible(r),
                (_, EvolutionVerdict::Incompatible(r)) => EvolutionVerdict::Incompatible(r),
                (EvolutionVerdict::Directional(r), _) => EvolutionVerdict::Directional(r),
                // Packed <-> unpacked for repeated scalars is accepted
                // transparently by the decoder, so always compatible.
                _ => EvolutionVerdict::Compatible,
            }
        }
        (None, None) => EvolutionVerdict::Compatible,
    }
}

/// Result of comparing two whole message schemas.
#[derive(Debug, Clone)]
pub struct EvolutionReport {
    pub message: String,
    pub compatible: bool,
    pub notes: Vec<(u32, String, EvolutionVerdict)>,
}

/// Compare every field of two versions of a message type. The comparison
/// is symmetric over the union of field numbers.
pub fn check_message_evolution(
    message_name: &str,
    old: &MessageDef,
    new: &MessageDef,
) -> EvolutionReport {
    let mut numbers: Vec<u32> = old.fields.keys().copied().collect();
    for n in new.fields.keys() {
        if !numbers.contains(n) {
            numbers.push(*n);
        }
    }
    numbers.sort_unstable();

    let mut notes = Vec::new();
    let mut compatible = true;
    for n in numbers {
        let verdict = field_evolution(n, old.fields.get(&n), new.fields.get(&n));
        let name = old
            .fields
            .get(&n)
            .or_else(|| new.fields.get(&n))
            .map(|f| f.name.clone())
            .unwrap_or_else(|| format!("field {n}"));
        if matches!(verdict, EvolutionVerdict::Incompatible(_)) {
            compatible = false;
        }
        notes.push((n, name, verdict));
    }
    EvolutionReport {
        message: message_name.to_string(),
        compatible,
        notes,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn scalar(ty: ScalarType, card: Cardinality) -> TypeDef {
        let _ = card;
        TypeDef::Scalar(ty)
    }

    #[test]
    fn widening_cardinality_is_compatible() {
        assert_eq!(
            cardinality_compatible(Cardinality::Required, Cardinality::Optional),
            EvolutionVerdict::Compatible
        );
        assert_eq!(
            cardinality_compatible(Cardinality::Optional, Cardinality::Repeated),
            EvolutionVerdict::Compatible
        );
    }

    #[test]
    fn narrowing_cardinality_is_rejected() {
        assert!(matches!(
            cardinality_compatible(Cardinality::Optional, Cardinality::Required),
            EvolutionVerdict::Incompatible(_)
        ));
        assert!(matches!(
            cardinality_compatible(Cardinality::Repeated, Cardinality::Optional),
            EvolutionVerdict::Incompatible(_)
        ));
    }

    #[test]
    fn adding_required_field_is_incompatible() {
        let f = FieldDef::new(
            "x",
            7,
            scalar(ScalarType::Int32, Cardinality::Required),
            Cardinality::Required,
        );
        assert!(matches!(
            field_evolution(7, None, Some(&f)),
            EvolutionVerdict::Incompatible(_)
        ));
    }

    #[test]
    fn adding_optional_field_is_compatible() {
        let f = FieldDef::new(
            "x",
            7,
            TypeDef::Scalar(ScalarType::Int32),
            Cardinality::Optional,
        );
        assert_eq!(
            field_evolution(7, None, Some(&f)),
            EvolutionVerdict::Compatible
        );
    }

    #[test]
    fn int_widening_is_compatible_but_string_to_int_is_not() {
        assert_eq!(
            wire_compatible_scalar(ScalarType::Int32, ScalarType::Int64),
            EvolutionVerdict::Compatible
        );
        let v = wire_compatible_scalar(ScalarType::String, ScalarType::Int32);
        assert!(matches!(v, EvolutionVerdict::Incompatible(_)));
    }

    #[test]
    fn schema_rejects_unknown_message_reference() {
        let mut fields = BTreeMap::new();
        fields.insert(
            1,
            FieldDef::new(
                "p",
                1,
                TypeDef::Message("Ghost".into()),
                Cardinality::Optional,
            ),
        );
        let msgs = BTreeMap::from([(
            "A".to_string(),
            MessageDef {
                name: "A".into(),
                fields,
            },
        )]);
        assert!(Schema::new(msgs, "A").is_err());
    }
}
