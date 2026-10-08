from __future__ import annotations

import base64
import json
import struct
import zlib
from typing import Literal

import pytest

from nanolab.functions.contracts import ContractCase


def http(body, status=200, headers=None):
    return {
        "status": status,
        "headers": headers or {"Content-Type": "application/json"},
        "bodyBase64": base64.b64encode(json.dumps(body).encode()).decode(),
        "executionId": "case-id",
    }


def callback(output, **extra):
    return {
        "executionId": "case-id",
        "method": "POST",
        "path": "/v1/executions/case-id:complete",
        "bodyBase64": base64.b64encode(
            json.dumps({"success": True, "output": output, **extra}).encode()
        ).decode(),
    }


def validate(
    case,
    *,
    mode: Literal["sdk", "warm", "one-shot"] = "sdk",
    response=None,
    callbacks=(),
    exit_code=None,
):
    from nanolab.tasks.validation.function_contracts import validate_case_observation

    return validate_case_observation(
        case, mode=mode, http=response, callbacks=callbacks, exit_code=exit_code
    )


def test_boolean_does_not_match_integer():
    case = ContractCase("integer", {}, {"answer": 1}, 200, None)
    with pytest.raises(ValueError, match="body"):
        validate(
            case,
            response=http({"answer": True}),
            callbacks=(callback({"answer": True}),),
        )


def test_missing_status_cannot_qualify_422():
    case = ContractCase("error", {}, {"error": "text"}, 422, None)
    with pytest.raises(ValueError, match="status"):
        validate(
            case, mode="one-shot", callbacks=(callback(case.expected),), exit_code=0
        )


def test_missing_status_means_200():
    case = ContractCase("normal", {}, {"answer": 1}, 200, None)
    validate(case, response=http({"answer": 1}), callbacks=(callback({"answer": 1}),))


@pytest.mark.parametrize(
    ("body", "status"),
    [({"answer": 2}, 200), ({"answer": [2, 1]}, 200), ({"answer": [1, 2]}, 500)],
)
def test_wrong_status_body_and_array_order_fail(body, status):
    case = ContractCase("ordered", {}, {"answer": [1, 2]}, 200, None)
    with pytest.raises(ValueError, match=r"body|status"):
        validate(case, response=http(body, status), callbacks=(callback(body),))


def test_business_error_200_is_valid():
    case = ContractCase("business", {}, {"error": "required"}, 200, None)
    validate(case, response=http(case.expected), callbacks=(callback(case.expected),))


def test_runtime_error_envelope_fails():
    case = ContractCase("business", {}, {"error": "required"}, 200, None)
    with pytest.raises(ValueError, match="callback"):
        validate(
            case,
            response=http(case.expected),
            callbacks=(
                callback(case.expected, success=False, error={"code": "HANDLER_ERROR"}),
            ),
        )


@pytest.mark.parametrize("fault", ["missing", "duplicate", "wrong-id"])
def test_missing_duplicate_wrong_id_callbacks_fail(fault):
    case = ContractCase("normal", {}, {}, 200, None)
    observed = (callback({}),)
    if fault == "missing":
        observed = ()
    elif fault == "duplicate":
        observed = (*observed, *observed)
    else:
        observed[0]["executionId"] = "other"
    with pytest.raises(ValueError, match="callback"):
        validate(case, response=http({}), callbacks=observed)


def test_warm_has_zero_callbacks():
    case = ContractCase("warm", {}, {}, 200, None)
    validate(case, mode="warm", response=http({}))
    with pytest.raises(ValueError, match="callback"):
        validate(case, mode="warm", response=http({}), callbacks=(callback({}),))


def test_nonzero_one_shot_exit_fails():
    case = ContractCase("normal", {}, {}, 200, None)
    with pytest.raises(ValueError, match="exit"):
        validate(case, mode="one-shot", callbacks=(callback({}),), exit_code=1)


def png(size=256, pixel=0):
    def chunk(kind, body):
        return (
            struct.pack("!I", len(body))
            + kind
            + body
            + struct.pack("!I", zlib.crc32(kind + body))
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack("!IIBBBBB", size, size, 8, 0, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress((b"\0" + bytes([pixel]) * size) * size))
        + chunk(b"IEND", b"")
    )


@pytest.mark.parametrize("fault", ["valid", "crc", "dimensions", "base64"])
def test_png_crc_dimensions_and_base64(fault):
    case = ContractCase("png", {"text": "x"}, None, 200, 256)
    raw = png(128 if fault == "dimensions" else 256)
    if fault == "crc":
        raw = raw[:-1] + bytes([raw[-1] ^ 1])
    encoded = "!invalid!" if fault == "base64" else base64.b64encode(raw).decode()
    headers = {
        "Content-Type": "image/png",
        "X-NanoFaaS-Encoding": "base64",
        "X-NanoFaaS-Function-Status": "true",
    }
    args = {
        "response": http(encoded, headers=headers),
        "callbacks": (
            callback(
                encoded,
                statusCode=200,
                headers={"Content-Type": "image/png"},
                encoding="base64",
            ),
        ),
    }
    if fault == "valid":
        validate(case, **args)
    else:
        with pytest.raises(ValueError, match=r"PNG|base64"):
            validate(case, **args)


def test_qr_http_and_callback_bytes_must_match():
    case = ContractCase("png", {"text": "x"}, None, 200, 256)
    headers = {
        "Content-Type": "image/png",
        "X-NanoFaaS-Encoding": "base64",
        "X-NanoFaaS-Function-Status": "true",
    }
    first, second = (base64.b64encode(png(pixel=pixel)).decode() for pixel in (0, 255))
    with pytest.raises(ValueError, match=r"PNG.*differ"):
        validate(
            case,
            response=http(first, headers=headers),
            callbacks=(
                callback(
                    second,
                    statusCode=200,
                    headers={"Content-Type": "image/png"},
                    encoding="base64",
                ),
            ),
        )


@pytest.mark.parametrize(
    "marker",
    [
        "MissingReflectionRegistrationError",
        "No serializer found",
        "Traceback (most recent call last)",
        "panicked at",
    ],
)
def test_logs_fail_on_native_serialization_traceback_panic(marker):
    from nanolab.tasks.validation.function_contracts import check_contract_logs

    with pytest.raises(ValueError, match="diagnostic"):
        check_contract_logs(marker.encode(), limit=8388608)


@pytest.mark.parametrize("fault", ["invalid-name", "reserved-bit", "split-data"])
def test_png_invalid_chunk_structure(fault):
    from nanolab.tasks.validation.function_contracts import validate_png

    def chunk(kind, body):
        return (
            struct.pack("!I", len(body))
            + kind
            + body
            + struct.pack("!I", zlib.crc32(kind + body))
        )

    raw = png()
    if fault == "split-data":
        compressed = zlib.compress((b"\0" + bytes(256)) * 256)
        middle = len(compressed) // 2
        raw = (
            raw[:33]
            + chunk(b"IDAT", compressed[:middle])
            + chunk(b"tEXt", b"key\0value")
            + chunk(b"IDAT", compressed[middle:])
            + raw[-12:]
        )
    else:
        kind = b"t1Xt" if fault == "invalid-name" else b"texT"
        raw = raw[:33] + chunk(kind, b"") + raw[33:]
    with pytest.raises(ValueError, match="PNG"):
        validate_png(raw, size=256)


def test_failed_transport_prefix_cannot_qualify():
    case = ContractCase("normal", {}, {}, 200, None)
    response = http({})
    response.update(error="request deadline", truncated=True)
    with pytest.raises(ValueError, match=r"probe|transport|truncated"):
        validate(case, response=response, callbacks=(callback({}),))
