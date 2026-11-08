# PackAPI 音频 API

Cube 服务端调用 `https://packapi.top`。提交、查询和模型列表都使用 `Authorization: Bearer <PackAPI API Key>`。`GET /v1/models` 返回当前 Key 可用的模型。调用时可发送 `X-Usage-Tag` 和 `X-Usage-Ref`，分别传 Cube 空间 ID 和 Run ID，便于对账。

三个模型均已在 PackAPI 生产启用。使用 `default` 分组的 Key 调用，若 Key 启用了模型白名单，请允许对应的模型 ID。

| 用途 | 模型 ID | 方法和路径 | 响应 |
| --- | --- | --- | --- |
| 语音、音效与配乐生成 | `seed-audio-1.0` | `POST /doubao/api/v3/tts/create` | JSON，含下载 URL 和生成时长 |
| 文本转语音 | `seed-tts-2.0` | `POST /doubao/api/v3/tts/unidirectional` | JSON，含按顺序排列的 MP3 分片 |
| 提交录音转写 | `volc.seedasr.auc` | `POST /doubao/api/v3/auc/bigmodel/submit` | JSON，含 PackAPI 任务 ID |
| 查询录音转写 | `volc.seedasr.auc` | `GET /doubao/api/v3/auc/bigmodel/tasks/{id}` | JSON，含状态和已完成的转写文本 |

以下示例在服务端环境中读取 `PACKAPI_API_KEY`。

```bash
PACKAPI_BASE_URL=https://packapi.top
```

## 用提示词和参考素材生成音频

`text_prompt` 最多 3,000 字符。`audio_config` 可指定格式和采样率。响应中的 `original_duration` 是计费秒数，`duration` 是后处理后的时长。`url` 是有效期两小时的签名链接，收到后应下载并保存到 Cube 文件存储。下载该链接时无需发送 PackAPI Key。 [豆包音频生成参数](https://docs.volcengine.com/docs/doubao-voice/audio-generation-http?lang=zh)

```bash
curl --fail-with-body --silent --show-error \
  -H "Authorization: Bearer ${PACKAPI_API_KEY}" \
  -H 'Content-Type: application/json' \
  --data-binary '{"model":"seed-audio-1.0","text_prompt":"生成一声清脆的风铃，接着是轻微的风声。","audio_config":{"format":"mp3","sample_rate":24000}}' \
  "${PACKAPI_BASE_URL}/doubao/api/v3/tts/create" > generated.json

curl --fail --silent --show-error "$(jq -er '.url' generated.json)" --output generated.mp3
```

使用参考音频时，传 `references: [{"audio_url":"https://media.example.com/reference.mp3"}]`，并在提示词中用 `@音频1` 引用第一条素材。使用参考图片时，传 `references: [{"image_url":"https://media.example.com/scene.png"}]`。本地文件也可读为原始 Base64 文本，分别放入 `audio_data` 或 `image_data`，省略 data URL 前缀。图片参考与音频参考分开提交。参考音频最多三条，每条最多 30 秒和 10 MB。参考图片最多一张，最大 10 MB。 [参考素材规则](https://docs.volcengine.com/docs/doubao-voice/audio-generation-http?lang=zh)

## 用指定音色朗读文本

`input` 为待朗读文本，最多 1,000 字符。`voice` 默认 `zh_female_vv_uranus_bigtts`，可使用豆包 TTS 2.0 音色 ID。`speech_rate` 为可选整数，范围 −50 至 100，默认 0。PackAPI 等上游合成结束后一次返回 JSON，包含 `audio_chunks`、`mime_type`、`sample_rate`、`characters`。将每个分片单独解码为二进制，再按顺序拼接保存为 MP3。 [音色列表](https://docs.volcengine.com/docs/doubao-voice/tone-list-3?lang=zh) · [豆包 TTS 接口](https://docs.volcengine.com/docs/doubao-voice/unidirectional-streaming-text-to-speech-http?lang=zh)

```bash
curl --fail-with-body --silent --show-error \
  -H "Authorization: Bearer ${PACKAPI_API_KEY}" \
  -H 'Content-Type: application/json' \
  --data-binary '{"model":"seed-tts-2.0","input":"你好，这是 Cube 的语音测试。","voice":"zh_female_vv_uranus_bigtts"}' \
  "${PACKAPI_BASE_URL}/doubao/api/v3/tts/unidirectional" > speech.json

python3 - <<'PY'
import base64
import json
from pathlib import Path

speech = json.loads(Path("speech.json").read_text())
Path("speech.mp3").write_bytes(b"".join(base64.b64decode(c) for c in speech["audio_chunks"]))
PY
```

## 转写本地录音

先把文件上传到 Cube 对象存储，再生成豆包服务可下载的 HTTPS URL。提交 `audio_url` 和 `format`（`mp3`、`wav` 或 `ogg`）。可选 `language` 使用 `zh-CN`、`en-US` 等语种代码。上游接受最长 5 小时且不超过 512 MB 的录音，签名 URL 要覆盖排队和转写。 [豆包录音文件识别](https://docs.volcengine.com/docs/doubao-voice/task-submission-http-1?lang=zh)

```bash
curl --fail-with-body --silent --show-error \
  -H "Authorization: Bearer ${PACKAPI_API_KEY}" \
  -H 'Content-Type: application/json' \
  --data-binary '{"model":"volc.seedasr.auc","audio_url":"https://media.example.com/recording.mp3","format":"mp3","language":"zh-CN"}' \
  "${PACKAPI_BASE_URL}/doubao/api/v3/auc/bigmodel/submit" > transcription.json

TASK_ID=$(jq -er '.id' transcription.json)
curl --fail-with-body --silent --show-error \
  -H "Authorization: Bearer ${PACKAPI_API_KEY}" \
  "${PACKAPI_BASE_URL}/doubao/api/v3/auc/bigmodel/tasks/${TASK_ID}"
```

保存返回的 `id`，每 5–15 秒查询一次。`queued` 和 `in_progress` 继续轮询，`completed` 时读取 `text` 和 `duration_seconds`，`failed` 时结束并记录 `error`。查询使用 PackAPI 任务 ID，查询动作本身无生成费。提交超时且未拿到 ID 时，先查任务或使用日志再决定是否重试，避免重复产生费用。

## 计费和错误

| 模型 | PackAPI `default` 分组单价 | 结算依据 |
| --- | --- | --- |
| Seed Audio 1.0 | $1／分钟 | 上游 `original_duration` |
| Seed TTS 2.0 | $3／万字符 | 上游实际计费字符数 `usage.text_words` |
| Seed ASR 录音文件 2.0 | $0.80／小时 | 上游 `audio_info.duration`，以毫秒换算小时 |

美元字段使用火山引擎人民币刊例的原数值。Cube 每积分对应 5,000 PackAPI quota，即 PackAPI $0.01。录音转写提交时需要足够额度预留 5 小时上限（400 积分），成功后按实际时长结算并退还差额，失败任务退款。 [豆包语音官方刊例](https://docs.volcengine.com/docs/DoubaoVoice/Billinginstructions-21?lang=zh)

提交失败时检查 HTTP 状态和 JSON `error`。处理成功的异步任务以查询响应的 `status` 为准。排查时记录 PackAPI 请求 ID 和任务 ID，避免记录 API Key、带签名的素材地址或完整 Base64 音频。

2026-11-08，PackAPI 生产版本 `357951c8b1d5` 的实测结果为语音合成 12 字符扣 1,800 quota（0.36 积分），录音转写 6.312 秒退差后净扣 701 quota（约 0.14 积分）。合成 MP3 已解码验证，转写成功返回文本，两笔账单均保留 `X-Usage-Tag` 和 `X-Usage-Ref`。
