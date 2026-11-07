# PackAPI 视频生成 API

Cube 服务端通过 [PackAPI](https://packapi.top) 提交异步生成任务。创建、查询和下载都使用 `Authorization: Bearer <PackAPI API Key>`。用同一个 Key 调用 `GET /v1/models` 可以确认可用的模型 ID。

| 操作 | 方法和路径 | 响应 |
| --- | --- | --- |
| 创建任务 | `POST /v1/videos` | HTTP 200，JSON 中的 `id` 是 PackAPI 任务 ID |
| 查询状态 | `GET /v1/videos/{id}` | JSON，包括 `status` 和 `progress` |
| 下载成片 | `GET /v1/videos/{id}/content` | 视频二进制，默认输出为 MP4 |

| 模型 ID | `size` | `seconds` |
| --- | --- | --- |
| `doubao-seedance-2-0-260128` | `480p`、`720p`、`1080p`、`4k` | 4–15 的整数，或 `-1` |
| `doubao-seedance-2-5-260628` | `480p`、`720p`、`1080p` | 4–30 的整数，或 `-1` |
| `MiniMax-H3` | `768P`、`2K` | 4–15 的整数 |

Seedance 的 `-1` 让模型决定时长，视频编辑时则沿用参考视频时长。分辨率还受生成模式和输入素材限制。Cube 可以先用 `seconds=4` 验证，Seedance 用 `480p`，H3 用 `768P`。 [火山方舟参数](https://docs.volcengine.com/docs/ark/create-video-generation-task-api?lang=zh) · [MiniMax H3 参数](https://platform.minimax.io/docs/api-reference/video-generation-v2-create)

## 文生视频和参考素材

三个模型都从顶层 `prompt` 读提示词。文生视频用 JSON 提交 `model`、`prompt`、`seconds` 和 `size`，可在 `metadata.ratio` 指定宽高比，例如 `16:9`。Seedance 可以通过 `metadata.generate_audio` 控制是否生成声音。

素材用 `metadata.content` 数组传入，每项的 URL 放在对应类型的嵌套对象内。

| 用途 | `type` | `role` | URL 字段 |
| --- | --- | --- | --- |
| 首帧图 | `image_url` | `first_frame` | `image_url.url` |
| 尾帧图 | `image_url` | `last_frame` | `image_url.url` |
| 参考图 | `image_url` | `reference_image` | `image_url.url` |
| 参考视频 | `video_url` | `reference_video` | `video_url.url` |
| 参考音频 | `audio_url` | `reference_audio` | `audio_url.url` |

首尾帧和全模态参考分开提交。全模态请求中的参考图、视频和音频可以组合使用。Seedance 2.5 也可以只提供参考音频，Seedance 2.0 的参考音频需要搭配参考图或视频。参考素材 URL 应能由上游直接下载，签名 URL 要覆盖排队和生成时间。Seedance 支持在图像和音频的 URL 字段中使用 Base64 data URL，视频使用 URL。H3 的混合参考示例使用 HTTPS URL。 [Seedance 全模态指南](https://docs.volcengine.com/docs/ark/seedance-2-5?lang=zh) · [MiniMax H3 素材要求](https://platform.minimax.io/docs/api-reference/video-generation-v2-create)

## 上传本地首帧

在服务端环境设置 `PACKAPI_API_KEY`，把示例的 `first.png` 换成实际文件。三个模型都使用 `multipart/form-data` 的 `input_reference` 字段上传一张首帧图。可使用 PNG、JPEG 或 WebP，并声明对应的 MIME 类型。让 HTTP 客户端自动生成 multipart boundary。

```bash
PACKAPI_BASE_URL=https://packapi.top

curl --fail-with-body --silent --show-error \
  -H "Authorization: Bearer ${PACKAPI_API_KEY}" \
  --form-string 'model=doubao-seedance-2-0-260128' \
  --form-string 'prompt=固定镜头，橙色球从左往右滚动。' \
  --form-string 'seconds=4' \
  --form-string 'size=480p' \
  --form-string 'metadata_json={"ratio":"adaptive"}' \
  --form 'input_reference=@first.png;type=image/png' \
  "${PACKAPI_BASE_URL}/v1/videos" > task.json
```

同时指定尾帧时，把 `metadata_json` 换成下面对象序列化后的字符串。尾帧也可以用支持的图像 data URL。仅指定远程首尾帧时，可以直接用 JSON 的 `metadata.content`，省去 multipart 上传。

```json
{
  "ratio": "adaptive",
  "content": [
    {
      "type": "image_url",
      "role": "last_frame",
      "image_url": {"url": "https://media.example.com/last.png"}
    }
  ]
}
```

## 图片、视频和音频混合参考

把下面内容保存成 `request.json`，换成实际的参考素材 URL。对于 H3，将 `model` 改为 `MiniMax-H3`，`size` 改为 `768P`，并从 `metadata` 中移除 Seedance 专用的 `omni_reference_task_type` 和 `generate_audio`。对于 Seedance 2.0，改模型 ID 并移除 `omni_reference_task_type`。

```json
{
  "model": "doubao-seedance-2-5-260628",
  "prompt": "生成橙色球滚过浅木桌的全新镜头，参考图片的配色、视频的运动节奏和音频的环境声。",
  "seconds": 4,
  "size": "480p",
  "metadata": {
    "ratio": "16:9",
    "omni_reference_task_type": "reference",
    "generate_audio": true,
    "content": [
      {
        "type": "image_url",
        "role": "reference_image",
        "image_url": {"url": "https://media.example.com/ball.png"}
      },
      {
        "type": "video_url",
        "role": "reference_video",
        "video_url": {"url": "https://media.example.com/motion.mp4"}
      },
      {
        "type": "audio_url",
        "role": "reference_audio",
        "audio_url": {"url": "https://media.example.com/ambience.mp3"}
      }
    ]
  }
}
```

```bash
curl --fail-with-body --silent --show-error \
  -H "Authorization: Bearer ${PACKAPI_API_KEY}" \
  -H 'Content-Type: application/json' \
  --data-binary @request.json \
  "${PACKAPI_BASE_URL}/v1/videos" > task.json
```

如果客户端统一走 multipart，把 JSON 里的 `metadata` 对象序列化为表单字段 `metadata_json` 即可，`model`、`prompt`、`seconds`、`size` 仍分别传。一个请求使用一种 metadata 字段。

Seedance 2.5 还可透传视频编辑和延长参数。编辑时指定 `omni_reference_task_type: "edit"`、`ratio: "adaptive"`、`seconds: -1`，并传一段 4–30 秒的 `reference_video`。延长时用 `omni_reference_task_type: "extend"`、`ratio: "adaptive"` 和 `reference_video`，在提示词中写明延长方向。本轮线上测试覆盖了混合参考生成，编辑和延长参数按上游约束使用。 [Seedance 2.5 任务类型](https://docs.volcengine.com/docs/ark/seedance-2-5?lang=zh)

## 轮询和下载

保存创建响应中的 `id`。任务可能在 `queued` 或 `in_progress` 停留几分钟，可以每 10–15 秒查询一次。创建请求超时且未拿到 ID 时，先查 PackAPI 任务记录再决定是否重提，以免创建两个计费任务。

```bash
TASK_ID=$(jq -er '.id' task.json)

curl --fail-with-body --silent --show-error \
  -H "Authorization: Bearer ${PACKAPI_API_KEY}" \
  "${PACKAPI_BASE_URL}/v1/videos/${TASK_ID}"
```

`status: "completed"` 后下载视频并保存到 Cube 的文件存储。已完成的查询响应可以返回 `seconds`（字符串）和 `size`。收到 `status: "failed"` 时结束轮询，记录任务 ID 和返回的 `error`（如有）。

```bash
curl --fail-with-body --silent --show-error \
  -H "Authorization: Bearer ${PACKAPI_API_KEY}" \
  "${PACKAPI_BASE_URL}/v1/videos/${TASK_ID}/content" \
  --output result.mp4
```

2026 年 11 月 7 日在生产环境通过此接口验证了 Seedance 2.0 首尾帧、Seedance 2.5 图视频音频混合参考，以及 H3 的首尾帧和混合参考。四个任务都完成了提交、轮询、MP4 下载、解码和计费核对。
