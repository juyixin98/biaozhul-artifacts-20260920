package com.example.smj;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Minimal dependency-free JSON parser/serializer.
 *
 * <p>Numbers are preserved as their raw source text ({link Num}) so values round-trip
 * exactly and integer keys can be validated without floating-point conversion.
 * Objects parse into insertion-ordered {@link LinkedHashMap}.
 */
public final class Json {

  private Json() {}

  /** A JSON number kept as raw source text. */
  public record Num(String raw) {
    @Override
    public String toString() {
      return raw;
    }
  }

  /** Thrown on malformed JSON input. */
  public static class JsonException extends RuntimeException {
    public JsonException(String message) {
      super(message);
    }
  }

  public static Object parse(String text) {
    Parser p = new Parser(text);
    p.skipWs();
    Object v = p.parseValue();
    p.skipWs();
    if (!p.atEnd()) {
      throw p.err("trailing characters after JSON value");
    }
    return v;
  }

  public static String write(Object value) {
    StringBuilder sb = new StringBuilder();
    writeTo(value, sb);
    return sb.toString();
  }

  @SuppressWarnings("unchecked")
  private static void writeTo(Object v, StringBuilder sb) {
    if (v == null) {
      sb.append("null");
    } else if (v instanceof String s) {
      writeString(s, sb);
    } else if (v instanceof Num n) {
      sb.append(n.raw());
    } else if (v instanceof Boolean b) {
      sb.append(b);
    } else if (v instanceof Map<?, ?> m) {
      sb.append('{');
      boolean first = true;
      for (Map.Entry<?, ?> e : ((Map<Object, Object>) m).entrySet()) {
        if (!first) sb.append(',');
        first = false;
        writeString(String.valueOf(e.getKey()), sb);
        sb.append(':');
        writeTo(e.getValue(), sb);
      }
      sb.append('}');
    } else if (v instanceof List<?> l) {
      sb.append('[');
      boolean first = true;
      for (Object e : l) {
        if (!first) sb.append(',');
        first = false;
        writeTo(e, sb);
      }
      sb.append(']');
    } else if (v instanceof Number n) {
      sb.append(n);
    } else {
      throw new IllegalArgumentException("not JSON-serializable: " + v.getClass());
    }
  }

  private static void writeString(String s, StringBuilder sb) {
    sb.append('"');
    for (int i = 0; i < s.length(); i++) {
      char c = s.charAt(i);
      switch (c) {
        case '"' -> sb.append("\\\"");
        case '\\' -> sb.append("\\\\");
        case '\b' -> sb.append("\\b");
        case '\f' -> sb.append("\\f");
        case '\n' -> sb.append("\\n");
        case '\r' -> sb.append("\\r");
        case '\t' -> sb.append("\\t");
        default -> {
          if (c < 0x20) {
            sb.append(String.format("\\u%04x", (int) c));
          } else {
            sb.append(c);
          }
        }
      }
    }
    sb.append('"');
  }

  private static final class Parser {
    private final String s;
    private int pos;

    Parser(String s) {
      this.s = s;
    }

    boolean atEnd() {
      return pos >= s.length();
    }

    JsonException err(String msg) {
      return new JsonException(msg + " at position " + pos);
    }

    void skipWs() {
      while (pos < s.length()) {
        char c = s.charAt(pos);
        if (c == ' ' || c == '\t' || c == '\n' || c == '\r') pos++;
        else break;
      }
    }

    boolean peek(char c) {
      return pos < s.length() && s.charAt(pos) == c;
    }

    void expect(char c) {
      if (!peek(c)) throw err("expected '" + c + "'");
      pos++;
    }

    void expect(String word) {
      if (!s.startsWith(word, pos)) throw err("expected '" + word + "'");
      pos += word.length();
    }

    Object parseValue() {
      if (atEnd()) throw err("unexpected end of input");
      char c = s.charAt(pos);
      return switch (c) {
        case '{' -> parseObject();
        case '[' -> parseArray();
        case '"' -> parseString();
        case 't' -> { expect("true"); yield Boolean.TRUE; }
        case 'f' -> { expect("false"); yield Boolean.FALSE; }
        case 'n' -> { expect("null"); yield null; }
        default -> parseNumber();
      };
    }

    Map<String, Object> parseObject() {
      pos++; // '{'
      Map<String, Object> m = new LinkedHashMap<>();
      skipWs();
      if (peek('}')) { pos++; return m; }
      while (true) {
        skipWs();
        if (!peek('"')) throw err("expected string key");
        String k = parseString();
        skipWs();
        expect(':');
        skipWs();
        m.put(k, parseValue());
        skipWs();
        if (peek(',')) { pos++; continue; }
        if (peek('}')) { pos++; return m; }
        throw err("expected ',' or '}'");
      }
    }

    List<Object> parseArray() {
      pos++; // '['
      List<Object> l = new ArrayList<>();
      skipWs();
      if (peek(']')) { pos++; return l; }
      while (true) {
        skipWs();
        l.add(parseValue());
        skipWs();
        if (peek(',')) { pos++; continue; }
        if (peek(']')) { pos++; return l; }
        throw err("expected ',' or ']'");
      }
    }

    String parseString() {
      pos++; // '"'
      StringBuilder sb = new StringBuilder();
      while (true) {
        if (atEnd()) throw err("unterminated string");
        char c = s.charAt(pos++);
        if (c == '"') return sb.toString();
        if (c == '\\') {
          if (atEnd()) throw err("unterminated escape");
          char e = s.charAt(pos++);
          switch (e) {
            case '"' -> sb.append('"');
            case '\\' -> sb.append('\\');
            case '/' -> sb.append('/');
            case 'b' -> sb.append('\b');
            case 'f' -> sb.append('\f');
            case 'n' -> sb.append('\n');
            case 'r' -> sb.append('\r');
            case 't' -> sb.append('\t');
            case 'u' -> {
              if (pos + 4 > s.length()) throw err("bad \\u escape");
              try {
                sb.append((char) Integer.parseInt(s.substring(pos, pos + 4), 16));
              } catch (NumberFormatException ex) {
                throw err("bad \\u escape");
              }
              pos += 4;
            }
            default -> throw err("bad escape '\\" + e + "'");
          }
        } else {
          sb.append(c);
        }
      }
    }

    Num parseNumber() {
      int start = pos;
      if (peek('-')) pos++;
      while (!atEnd()) {
        char c = s.charAt(pos);
        if ((c >= '0' && c <= '9') || c == '.' || c == 'e' || c == 'E' || c == '+' || c == '-') pos++;
        else break;
      }
      if (start == pos) throw err("unexpected character '" + s.charAt(pos) + "'");
      String raw = s.substring(start, pos);
      try {
        Double.parseDouble(raw);
      } catch (NumberFormatException e) {
        throw err("invalid number '" + raw + "'");
      }
      return new Num(raw);
    }
  }
}
