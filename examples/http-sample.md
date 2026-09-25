# HTTP 请求/响应样例（真实运行记录）

服务地址：`http://127.0.0.1:46743`（127.0.0.1，内核分配端口）

### 健康检查

请求：
```
GET /health
```

响应（HTTP 200）：
```
{
  "status": "ok"
}
```

### 无主密钥时加密（期望 409）

请求：
```
POST /encrypt

{"data_b64": "aGVsbG8="}
```

响应（HTTP 409）：
```
{
  "error": "密钥库为空：请先 create_master_key()"
}
```

### 生成主密钥

请求：
```
POST /keys
```

响应（HTTP 201）：
```
{
  "kid": "mk_d92d8d361b9f5d182e128768",
  "created_at": "2026-09-23T17:41:16Z"
}
```

### 加密 1000 字节明文（64B/块，16 块）

请求：
```
POST /encrypt

{"data_b64": "AAcOFRwjKjE4P0ZNVFtiaXB3foWMk5qhqK+2vcTL0tng5+71/AMKERgfJi00O0JJUFdeZWxzeoGIj5adpKuyucDHztXc4+rx+P8GDRQbIikwNz5FTFNaYWhvdn2Ei5KZoKeutbzDytHY3+bt9PsCCRAXHiUsMzpBSE9WXWRrcnmAh46VnKOqsbi/xs3U2+Lp8Pf+BQwTGiEoLzY9REtSWWBnbnV8g4qRmJ+mrbS7wsnQ197l7PP6AQgPFh0kKzI5QEdOVVxjanF4f4aNlJuiqbC3vsXM09rh6O/2/QQLEhkgJy41PENKUVhfZm10e4KJkJeepayzusHIz9bd5Ovy+QAHDhUcIyoxOD9GTVRbYmlwd36FjJOaoaivtr3Ey9LZ4Ofu9fwDChEYHyYtNDtCSVBXXmVsc3qBiI+WnaSrsrnAx87V3OPq8fj/Bg0UGyIpMDc+RUxTWmFob3Z9hIuSmaCnrrW8w8rR2N/m7fT7AgkQFx4lLDM6QUhPVl1ka3J5gIeOlZyjqrG4v8bN1Nvi6fD3/gUMExohKC82PURLUllgZ251fIOKkZifpq20u8LJ0Nfe5ezz+gEIDxYdJCsyOUBHTlVcY2pxeH+GjZSboqmwt77FzNPa4ejv9v0ECxIZICcuNTxDSlFYX2ZtdHuCiZCXnqWss7rByM/W3eTr8vkABw4VHCMqMTg/Rk1UW2JpcHd+hYyTmqGor7a9xMvS2eDn7vX8AwoRGB8mLTQ7QklQV15lbHN6gYiPlp2kq7K5wMfO1dzj6vH4/wYNFBsiKTA3PkVMU1phaG92fYSLkpmgp661vMPK0djf5u30+wIJEBceJSwzOkFIT1ZdZGtyeYCHjpWco6qxuL/GzdTb4unw9/4FDBMaISgvNj1ES1JZYGdudXyDipGYn6attLvCydDX3uXs8/oBCA8WHSQrMjlAR05VXGNqcXh/ho2Um6KpsLe+xczT2uHo7/b9BAsSGSAnLjU8Q0pRWF9mbXR7gomQl56lrLO6wcjP1t3k6/L5AAcOFRwjKjE4P0ZNVFtiaXB3foWMk5qhqK+2vcTL0tng5+71/AMKERgfJi00O0JJUFdeZWxzeoGIj5adpKuyucDHztXc4+rx+P8GDRQbIikwNz5FTFNaYWhvdn2Ei5KZoKeutbzDytHY3+bt9PsCCRAXHiUsMzpBSE9WXWRrcnmAh46VnKOqsbi/xs3U2+Lp8Pf+BQwTGiEoLzY9REtSWWBnbnV8g4qRmJ+mrbS7wsnQ197l7PP6AQgPFh0kKzI5QEdOVVxjanF4f4aNlJuiqbC3vsXM09rh6O/2/QQLEhkgJy41PENKUQ==", "chunk_size": 64}
```

响应（HTTP 201）：
```
{
  "file_id": "f4c5964b0a5f22fa8eb5a868bf430176",
  "plaintext_size": 1000,
  "chunk_size": 64,
  "blocks": 16,
  "wrapped_by_kid": "mk_d92d8d361b9f5d182e128768"
}
```

### 解密

请求：
```
GET /decrypt?file_id=f4c5964b0a5f22fa8eb5a868bf430176
```

响应（HTTP 200）：
```
{
  "file_id": "f4c5964b0a5f22fa8eb5a868bf430176",
  "data_b64": "AAcOFRwjKjE4P0ZNVFtiaXB3foWMk5qhqK+2vcTL0tng5+71/AMKERgfJi00O0JJUFdeZWxzeoGIj5adpKuyucDHztXc4+rx+P8GDRQbIikwNz5FTFNaYWhvdn2Ei5KZoKeutbzDytHY3+bt9PsCCRAXHiUsMzpBSE9WXWRrcnmAh46VnKOqsbi/xs3U2+Lp8Pf+BQwTGiEoLzY9REtSWWBnbnV8g4qRmJ+mrbS7wsnQ197l7PP6AQgPFh0kKzI5QEdOVVxjanF4f4aNlJuiqbC3vsXM09rh6O/2/QQLEhkgJy41PENKUVhfZm10e4KJkJeepayzusHIz9bd5Ovy+QAHDhUcIyoxOD9GTVRbYmlwd36FjJOaoaivtr3Ey9LZ4Ofu9fwDChEYHyYtNDtCSVBXXmVsc3qBiI+WnaSrsrnAx87V3OPq8fj/Bg0UGyIpMDc+RUxTWmFob3Z9hIuSmaCnrrW8w8rR2N/m7fT7AgkQFx4lLDM6QUhPVl1ka3J5gIeOlZyjqrG4v8bN1Nvi6fD3/gUMExohKC82PURLUllgZ251fIOKkZifpq20u8LJ0Nfe5ezz+gEIDxYdJCsyOUBHTlVcY2pxeH+GjZSboqmwt77FzNPa4ejv9v0ECxIZICcuNTxDSlFYX2ZtdHuCiZCXnqWss7rByM/W3eTr8vkABw4VHCMqMTg/Rk1UW2JpcHd+hYyTmqGor7a9xMvS2eDn7vX8AwoRGB8mLTQ7QklQV15lbHN6gYiPlp2kq7K5wMfO1dzj6vH4/wYNFBsiKTA3PkVMU1phaG92fYSLkpmgp661vMPK0djf5u30+wIJEBceJSwzOkFIT1ZdZGtyeYCHjpWco6qxuL/GzdTb4unw9/4FDBMaISgvNj1ES1JZYGdudXyDipGYn6attLvCydDX3uXs8/oBCA8WHSQrMjlAR05VXGNqcXh/ho2Um6KpsLe+xczT2uHo7/b9BAsSGSAnLjU8Q0pRWF9mbXR7gomQl56lrLO6wcjP1t3k6/L5AAcOFRwjKjE4P0ZNVFtiaXB3foWMk5qhqK+2vcTL0tng5+71/AMKERgfJi00O0JJUFdeZWxzeoGIj5adpKuyucDHztXc4+rx+P8GDRQbIikwNz5FTFNaYWhvdn2Ei5KZoKeutbzDytHY3+bt9PsCCRAXHiUsMzpBSE9WXWRrcnmAh46VnKOqsbi/xs3U2+Lp8Pf+BQwTGiEoLzY9REtSWWBnbnV8g4qRmJ+mrbS7wsnQ197l7PP6AQgPFh0kKzI5QEdOVVxjanF4f4aNlJuiqbC3vsXM09rh6O/2/QQLEhkgJy41PENKUQ==",
  "plaintext_size": 1000
}
```

### 轮换主密钥（期望 blob_bytes_changed=0）

请求：
```
POST /rotate

{"file_id": "f4c5964b0a5f22fa8eb5a868bf430176"}
```

响应（HTTP 200）：
```
{
  "file_id": "f4c5964b0a5f22fa8eb5a868bf430176",
  "old_kid": "mk_d92d8d361b9f5d182e128768",
  "new_kid": "mk_f76af2bd37dad7bbc7f51177",
  "header_version": 2,
  "blob_bytes_changed": 0
}
```

### 轮换后再解密

请求：
```
GET /decrypt?file_id=f4c5964b0a5f22fa8eb5a868bf430176
```

响应（HTTP 200）：
```
{
  "file_id": "f4c5964b0a5f22fa8eb5a868bf430176",
  "data_b64": "AAcOFRwjKjE4P0ZNVFtiaXB3foWMk5qhqK+2vcTL0tng5+71/AMKERgfJi00O0JJUFdeZWxzeoGIj5adpKuyucDHztXc4+rx+P8GDRQbIikwNz5FTFNaYWhvdn2Ei5KZoKeutbzDytHY3+bt9PsCCRAXHiUsMzpBSE9WXWRrcnmAh46VnKOqsbi/xs3U2+Lp8Pf+BQwTGiEoLzY9REtSWWBnbnV8g4qRmJ+mrbS7wsnQ197l7PP6AQgPFh0kKzI5QEdOVVxjanF4f4aNlJuiqbC3vsXM09rh6O/2/QQLEhkgJy41PENKUVhfZm10e4KJkJeepayzusHIz9bd5Ovy+QAHDhUcIyoxOD9GTVRbYmlwd36FjJOaoaivtr3Ey9LZ4Ofu9fwDChEYHyYtNDtCSVBXXmVsc3qBiI+WnaSrsrnAx87V3OPq8fj/Bg0UGyIpMDc+RUxTWmFob3Z9hIuSmaCnrrW8w8rR2N/m7fT7AgkQFx4lLDM6QUhPVl1ka3J5gIeOlZyjqrG4v8bN1Nvi6fD3/gUMExohKC82PURLUllgZ251fIOKkZifpq20u8LJ0Nfe5ezz+gEIDxYdJCsyOUBHTlVcY2pxeH+GjZSboqmwt77FzNPa4ejv9v0ECxIZICcuNTxDSlFYX2ZtdHuCiZCXnqWss7rByM/W3eTr8vkABw4VHCMqMTg/Rk1UW2JpcHd+hYyTmqGor7a9xMvS2eDn7vX8AwoRGB8mLTQ7QklQV15lbHN6gYiPlp2kq7K5wMfO1dzj6vH4/wYNFBsiKTA3PkVMU1phaG92fYSLkpmgp661vMPK0djf5u30+wIJEBceJSwzOkFIT1ZdZGtyeYCHjpWco6qxuL/GzdTb4unw9/4FDBMaISgvNj1ES1JZYGdudXyDipGYn6attLvCydDX3uXs8/oBCA8WHSQrMjlAR05VXGNqcXh/ho2Um6KpsLe+xczT2uHo7/b9BAsSGSAnLjU8Q0pRWF9mbXR7gomQl56lrLO6wcjP1t3k6/L5AAcOFRwjKjE4P0ZNVFtiaXB3foWMk5qhqK+2vcTL0tng5+71/AMKERgfJi00O0JJUFdeZWxzeoGIj5adpKuyucDHztXc4+rx+P8GDRQbIikwNz5FTFNaYWhvdn2Ei5KZoKeutbzDytHY3+bt9PsCCRAXHiUsMzpBSE9WXWRrcnmAh46VnKOqsbi/xs3U2+Lp8Pf+BQwTGiEoLzY9REtSWWBnbnV8g4qRmJ+mrbS7wsnQ197l7PP6AQgPFh0kKzI5QEdOVVxjanF4f4aNlJuiqbC3vsXM09rh6O/2/QQLEhkgJy41PENKUQ==",
  "plaintext_size": 1000
}
```

### 列出文件

请求：
```
GET /files
```

响应（HTTP 200）：
```
{
  "files": [
    {
      "file_id": "f4c5964b0a5f22fa8eb5a868bf430176",
      "wrapped_by_kid": "mk_f76af2bd37dad7bbc7f51177",
      "wrapping_key_created_at": "2026-09-23T17:41:16Z",
      "header_version": 2,
      "algorithm": "AES-256-GCM",
      "plaintext_size": 1000,
      "chunk_size": 64,
      "blocks": 16
    }
  ]
}
```

### 篡改密文后解密（期望 422 AEAD 失败）

请求：
```
GET /decrypt?file_id=f4c5964b0a5f22fa8eb5a868bf430176
```

响应（HTTP 422）：
```
{
  "error": "AEAD 认证失败：密文/nonce/关联数据被篡改，或密钥不正确"
}
```

### 请求不存在的文件（期望 404）

请求：
```
GET /decrypt?file_id=00000000000000000000000000000000
```

响应（HTTP 404）：
```
{
  "error": "信封元数据不存在：00000000000000000000000000000000"
}
```
