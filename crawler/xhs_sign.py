"""
XHS 签名模块 —— 参考 MediaCrawler (NanmiCoder/MediaCrawler)

使用 xhshow 纯算法库生成 x-s / x-t / x-s-common / x-b3-traceid。
无需 JS 逆向，无需浏览器，完全本地计算，随 xhshow 库自动更新。

致谢：xhshow 库由 Cloxl 提供 (https://github.com/Cloxl/xhshow)
"""
from __future__ import annotations

import hashlib
import json
import time
import random
import string
from typing import Any, Dict, Optional, Union
from urllib.parse import quote


# ── monkey-patch：修复 xhshow GET 请求 a3_hash 计算 bug ────────
# 参考：https://github.com/Cloxl/xhshow/issues/104
def _patch_xhshow_a3_hash():
    try:
        from xhshow.core.crypto import CryptoProcessor
        _original_build = CryptoProcessor.build_payload_array

        def _patched_build(self, hex_parameter, a1_value, app_identifier="xhs-pc-web",
                           string_param="", timestamp=None, sign_state=None):
            payload = _original_build(self, hex_parameter, a1_value, app_identifier,
                                      string_param, timestamp, sign_state)
            if "{" not in string_param:
                correct_md5_hex = hashlib.md5(string_param.encode("utf-8")).hexdigest()
                correct_md5_bytes = [int(correct_md5_hex[i:i + 2], 16) for i in range(0, 32, 2)]
                seed_byte = payload[4]
                ts_bytes = payload[8:16]
                correct_a3_hash = self._custom_hash_v2(list(ts_bytes) + correct_md5_bytes)
                for i in range(16):
                    payload[128 + i] = correct_a3_hash[i] ^ seed_byte
            return payload

        CryptoProcessor.build_payload_array = _patched_build
    except Exception:
        pass


_patch_xhshow_a3_hash()


def get_trace_id() -> str:
    """生成 x-b3-traceid"""
    return "".join(random.choices(string.hexdigits[:16], k=16))


def _build_sign_string(uri: str, data: Optional[Union[Dict, str]], method: str) -> str:
    """构建待签名字符串"""
    if method.upper() == "POST":
        c = uri
        if data is not None:
            if isinstance(data, dict):
                c += json.dumps(data, separators=(",", ":"), ensure_ascii=False)
            elif isinstance(data, str):
                c += data
        return c
    else:
        if not data or (isinstance(data, dict) and len(data) == 0):
            return uri
        if isinstance(data, dict):
            parts = []
            for k, v in data.items():
                if isinstance(v, list):
                    v_str = ",".join(str(x) for x in v)
                elif v is not None:
                    v_str = str(v)
                else:
                    v_str = ""
                parts.append(f"{k}={quote(v_str, safe=',')}")
            return f"{uri}?{'&'.join(parts)}"
        elif isinstance(data, str):
            return f"{uri}?{data}"
        return uri


def sign_request(
    uri: str,
    data: Optional[Union[Dict, str]] = None,
    cookie_str: str = "",
    method: str = "POST",
) -> Dict[str, Any]:
    """
    生成完整 XHS 请求签名头。

    :param uri: API 路径，如 /api/sns/web/v1/search/notes
    :param data: GET 参数字典 或 POST body 字典
    :param cookie_str: 完整 Cookie 字符串
    :param method: 请求方法 GET/POST
    :return: {"x-s": ..., "x-t": ..., "x-s-common": ..., "x-b3-traceid": ...}
    """
    from xhshow import Xhshow
    client = Xhshow()
    is_post = method.upper() == "POST"

    if is_post:
        headers = client.sign_headers_post(
            uri=uri,
            cookies=cookie_str,
            payload=data if isinstance(data, dict) else {},
        )
    else:
        content_string = _build_sign_string(uri, data, method)
        cookie_dict = client._parse_cookies(cookie_str)
        a1_value = cookie_dict.get("a1", "")

        ts = time.time()
        d_value = hashlib.md5(content_string.encode("utf-8")).hexdigest()
        payload_array = client.crypto_processor.build_payload_array(
            d_value, a1_value, "xhs-pc-web", content_string, ts
        )
        xor_result = client.crypto_processor.bit_ops.xor_transform_array(payload_array)
        cfg = client.config
        x3_b64 = client.crypto_processor.b64encoder.encode_x3(xor_result[:cfg.PAYLOAD_LENGTH])
        sig_data = cfg.SIGNATURE_DATA_TEMPLATE.copy()
        sig_data["x3"] = cfg.X3_PREFIX + x3_b64
        x_s = cfg.XYS_PREFIX + client.crypto_processor.b64encoder.encode(
            json.dumps(sig_data, separators=(",", ":"), ensure_ascii=False)
        )
        headers = {
            "x-s": x_s,
            "x-s-common": client.sign_xs_common(cookie_dict),
            "x-t": str(client.get_x_t(ts)),
            "x-b3-traceid": client.get_b3_trace_id(),
        }

    return {
        "x-s": headers.get("x-s", ""),
        "x-t": headers.get("x-t", ""),
        "x-s-common": headers.get("x-s-common", ""),
        "x-b3-traceid": headers.get("x-b3-traceid", get_trace_id()),
    }


def make_sign_func(cookie_str: str):
    """
    返回一个符合 XhsClient(sign=...) 参数要求的签名函数。
    XhsClient 调用时传入 (uri, data, a1="", web_session="")。
    """
    def _sign(uri: str, data=None, a1: str = "", web_session: str = "") -> dict:
        result = sign_request(uri, data, cookie_str=cookie_str, method="POST")
        return {"x-s": result["x-s"], "x-t": result["x-t"], "x-s-common": result["x-s-common"]}
    return _sign
