package com.example.smj;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertInstanceOf;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.util.List;
import java.util.Map;
import org.junit.jupiter.api.Test;

class JsonTest {

  @Test
  @SuppressWarnings("unchecked")
  void parsesNestedObject() {
    Object v = Json.parse("{\"a\":1,\"b\":[true,null,\"x\"],\"c\":{\"d\":-2.5e3}}");
    Map<String, Object> m = assertInstanceOf(Map.class, v);
    assertEquals("1", ((Json.Num) m.get("a")).raw());
    List<Object> b = (List<Object>) m.get("b");
    assertEquals(Boolean.TRUE, b.get(0));
    assertNull(b.get(1));
    assertEquals("x", b.get(2));
    assertEquals("-2.5e3", ((Json.Num) ((Map<String, Object>) m.get("c")).get("d")).raw());
  }

  @Test
  void numbersRoundTripExactly() {
    assertEquals("[1,3.50,1e3,-0.25]", Json.write(Json.parse("[1,3.50,1e3,-0.25]")));
  }

  @Test
  void stringsEscapeAndUnescape() {
    Map<String, Object> m = Map.of("q", "a\"b\\c\nd\te\t汉");
    String text = Json.write(m);
    assertEquals(m, Json.parse(text));
  }

  @Test
  void rejectsMalformedInput() {
    assertThrows(Json.JsonException.class, () -> Json.parse("{\"a\":1} garbage"));
    assertThrows(Json.JsonException.class, () -> Json.parse("{a:1}"));
    assertThrows(Json.JsonException.class, () -> Json.parse("[1,2"));
    assertThrows(Json.JsonException.class, () -> Json.parse(""));
  }
}
