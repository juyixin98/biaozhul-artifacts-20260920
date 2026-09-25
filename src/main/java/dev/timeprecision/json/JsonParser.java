package dev.timeprecision.json;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/** Minimal recursive-descent JSON parser (RFC 8259). */
public final class JsonParser {

    private final String input;
    private int pos;

    private JsonParser(String input) {
        this.input = input;
    }

    public static JsonValue parse(String input) {
        if (input == null) {
            throw new JsonParseException("empty input");
        }
        JsonParser parser = new JsonParser(input);
        parser.skipWhitespace();
        JsonValue value = parser.parseValue();
        parser.skipWhitespace();
        if (parser.pos != parser.input.length()) {
            throw new JsonParseException("trailing characters at offset " + parser.pos);
        }
        return value;
    }

    private JsonValue parseValue() {
        if (pos >= input.length()) {
            throw error("unexpected end of input");
        }
        char c = input.charAt(pos);
        return switch (c) {
            case '{' -> parseObject();
            case '[' -> parseArray();
            case '"' -> new JsonValue.Str(parseString());
            case 't' -> parseLiteral("true", new JsonValue.Bool(true));
            case 'f' -> parseLiteral("false", new JsonValue.Bool(false));
            case 'n' -> parseLiteral("null", JsonValue.Null.INSTANCE);
            default -> {
                if (c == '-' || (c >= '0' && c <= '9')) {
                    yield parseNumber();
                }
                throw error("unexpected character '" + c + "'");
            }
        };
    }

    private JsonValue parseObject() {
        expect('{');
        Map<String, JsonValue> members = new LinkedHashMap<>();
        skipWhitespace();
        if (peek('}')) {
            pos++;
            return new JsonValue.Obj(members);
        }
        while (true) {
            skipWhitespace();
            String key = parseString();
            skipWhitespace();
            expect(':');
            skipWhitespace();
            members.put(key, parseValue());
            skipWhitespace();
            if (peek(',')) {
                pos++;
                continue;
            }
            expect('}');
            return new JsonValue.Obj(members);
        }
    }

    private JsonValue parseArray() {
        expect('[');
        List<JsonValue> items = new ArrayList<>();
        skipWhitespace();
        if (peek(']')) {
            pos++;
            return new JsonValue.Arr(items);
        }
        while (true) {
            skipWhitespace();
            items.add(parseValue());
            skipWhitespace();
            if (peek(',')) {
                pos++;
                continue;
            }
            expect(']');
            return new JsonValue.Arr(items);
        }
    }

    private String parseString() {
        expect('"');
        StringBuilder sb = new StringBuilder();
        while (pos < input.length()) {
            char c = input.charAt(pos++);
            if (c == '"') {
                return sb.toString();
            }
            if (c == '\\') {
                sb.append(parseEscape());
            } else {
                sb.append(c);
            }
        }
        throw error("unterminated string");
    }

    private char parseEscape() {
        if (pos >= input.length()) {
            throw error("unterminated escape");
        }
        char e = input.charAt(pos++);
        return switch (e) {
            case '"' -> '"';
            case '\\' -> '\\';
            case '/' -> '/';
            case 'b' -> '\b';
            case 'f' -> '\f';
            case 'n' -> '\n';
            case 'r' -> '\r';
            case 't' -> '\t';
            case 'u' -> parseUnicodeEscape();
            default -> throw error("invalid escape '\\" + e + "'");
        };
    }

    private char parseUnicodeEscape() {
        if (pos + 4 > input.length()) {
            throw error("truncated \\u escape");
        }
        String hex = input.substring(pos, pos + 4);
        try {
            pos += 4;
            return (char) Integer.parseInt(hex, 16);
        } catch (NumberFormatException ex) {
            throw error("invalid \\u escape: " + hex);
        }
    }

    private JsonValue parseNumber() {
        int start = pos;
        if (peek('-')) {
            pos++;
        }
        while (pos < input.length() && isNumberChar(input.charAt(pos))) {
            pos++;
        }
        String raw = input.substring(start, pos);
        if (!raw.matches("-?(0|[1-9]\\d*)(\\.\\d+)?([eE][+-]?\\d+)?")) {
            throw error("malformed number: " + raw);
        }
        return new JsonValue.Num(raw);
    }

    private static boolean isNumberChar(char c) {
        return (c >= '0' && c <= '9') || c == '.' || c == 'e' || c == 'E' || c == '+' || c == '-';
    }

    private JsonValue parseLiteral(String literal, JsonValue value) {
        if (input.startsWith(literal, pos)) {
            pos += literal.length();
            return value;
        }
        throw error("invalid literal at offset " + pos);
    }

    private void skipWhitespace() {
        while (pos < input.length()) {
            char c = input.charAt(pos);
            if (c == ' ' || c == '\t' || c == '\n' || c == '\r') {
                pos++;
            } else {
                return;
            }
        }
    }

    private boolean peek(char c) {
        return pos < input.length() && input.charAt(pos) == c;
    }

    private void expect(char c) {
        if (!peek(c)) {
            throw error("expected '" + c + "' at offset " + pos);
        }
        pos++;
    }

    private JsonParseException error(String message) {
        return new JsonParseException(message + " (offset " + pos + ")");
    }
}
