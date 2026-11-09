const TTS = "seed-tts-2.0";
const ASR = "volc.seedasr.auc";
const DEFAULT_VOICE = "zh_female_vv_uranus_bigtts";
const MAX_CHARACTERS = 1000;
const MAX_ASR_HOURS = 5;

export const meta = {
  apiVersion: 1,
  key: "doubao-speech",
  name: "Doubao Speech",
  description: {
    en: "Synthesize speech and transcribe recordings with Doubao Speech 2.0",
    zh: "使用豆包语音 2.0 合成语音和转写录音文件",
  },
  version: "1.1.1",
  author: { name: "PackAPI" },
  icon: "Doubao.Color",
  baseUrl: "https://openspeech.bytedance.com",
  models: [TTS, ASR],
  fetchMode: "per_task",
  usageProfiles: [
    {
      models: [TTS],
      schema: { kilochars: { type: "number", unit: "count", unitLabel: { en: "1,000 characters", zh: "千字符" }, description: { en: "Speech synthesis unit price", zh: "语音合成单价" } } },
      examples: [{ label: "100 characters", facts: { kilochars: 0.1 } }],
    },
    {
      models: [ASR],
      schema: { hours: { type: "number", unit: "count", unitLabel: { en: "hour", zh: "小时" }, description: { en: "Recording transcription unit price", zh: "录音转写单价" } } },
      examples: [{ label: "1 minute", facts: { hours: 1 / 60 } }],
    },
  ],
  routes: [
    { method: "POST", path: "/doubao/api/v3/tts/unidirectional", type: "submit", decode: "speak", render: "speech", models: [TTS], retainResult: false },
    { method: "POST", path: "/doubao/api/v3/auc/bigmodel/submit", type: "submit", decode: "transcribe", render: "transcription", models: [ASR] },
    { method: "GET", path: "/doubao/api/v3/auc/bigmodel/tasks/:task_id", type: "query", taskIdParam: "task_id", render: "transcription" },
  ],
};

function requestBody(ctx, model) {
  const body = ctx.body.kind === "json" ? ctx.body.value : null;
  if (!body || typeof body !== "object" || Array.isArray(body)) throw new Error("JSON object required");
  if (body.model !== model) throw new Error("model must be " + model);
  return body;
}

export const native = {
  speak(ctx) {
    const body = requestBody(ctx, TTS);
    const text = body.input;
    if (typeof text !== "string" || !text.trim() || Array.from(text).length > MAX_CHARACTERS) {
      throw new Error("input must contain 1 to 1000 characters");
    }
    const speaker = body.voice === undefined ? DEFAULT_VOICE : body.voice;
    if (typeof speaker !== "string" || !/^[A-Za-z0-9_]{1,100}$/.test(speaker)) throw new Error("invalid voice ID");
    const speechRate = body.speech_rate === undefined ? 0 : body.speech_rate;
    if (!Number.isInteger(speechRate) || speechRate < -50 || speechRate > 100) throw new Error("speech_rate must be an integer from -50 to 100");
    return { kind: "submit", model: TTS, action: "synthesize_speech", requestBody: { text, speaker, speechRate } };
  },
  speech(_ctx, task) { return task.data; },
  transcribe(ctx) {
    const body = requestBody(ctx, ASR);
    if (typeof body.audio_url !== "string" || body.audio_url.length > 4096 || !/^https:\/\/[^\s]+$/i.test(body.audio_url)) {
      throw new Error("audio_url must be an HTTPS file URL");
    }
    if (!["mp3", "wav", "ogg", "m4a", "aac"].includes(body.format)) throw new Error("format must be mp3, wav, ogg, m4a, or aac");
    const audio = { url: body.audio_url, format: body.format };
    if (body.language !== undefined) {
      if (typeof body.language !== "string" || !/^[a-z]{2,3}-[A-Z]{2}$/.test(body.language)) throw new Error("language must be a language-region code");
      audio.language = body.language;
    }
    return { kind: "submit", model: ASR, action: "transcribe_audio", requestBody: { audio, requestId: utils.uuid() } };
  },
  transcription(_ctx, task) {
    const status = { SUCCESS: "completed", FAILURE: "failed", IN_PROGRESS: "in_progress" }[task.status] || "queued";
    const out = { id: task.task_id, model: ASR, status };
    if (status === "completed") {
      const result = task.data.result;
      out.text = result.text;
      out.duration_seconds = task.data.audio_info.duration / 1000;
      out.utterances = [];
      if (Array.isArray(result.utterances)) {
        for (const part of result.utterances) {
          if (!part || typeof part.text !== "string" || !Number.isInteger(part.start_time) || !Number.isInteger(part.end_time)
              || part.start_time < 0 || part.end_time <= part.start_time) continue;
          out.utterances.push({ start_ms: part.start_time, end_ms: part.end_time, text: part.text });
        }
      }
    } else if (status === "failed") {
      out.error = task.fail_reason || "Transcription failed";
    }
    return out;
  },
  error(_ctx, error) { return { error: { code: error.code, message: error.message } }; },
};

function headers(ctx, model, requestId) {
  return { "Content-Type": "application/json", "X-Api-Key": ctx.apiKey, "X-Api-Resource-Id": model, "X-Api-Request-Id": requestId };
}

export function buildSubmitRequest(ctx) {
  const req = ctx.requestBody;
  if (ctx.model === TTS) {
    return {
      url: ctx.baseUrl + "/api/v3/tts/unidirectional",
      method: "POST",
      headers: { ...headers(ctx, TTS, utils.uuid()), "X-Control-Require-Usage-Tokens-Return": "*" },
      body: { req_params: { text: req.text, speaker: req.speaker, audio_params: { format: "mp3", sample_rate: 24000, speech_rate: req.speechRate } } },
    };
  }
  return {
    url: ctx.baseUrl + "/api/v3/auc/bigmodel/submit",
    method: "POST",
    headers: { ...headers(ctx, ASR, req.requestId), "X-Api-Sequence": "-1" },
    body: { audio: req.audio, request: { model_name: "bigmodel", enable_itn: true, enable_punc: true, show_utterances: true } },
  };
}

function asrCode(resp) {
  const header = Object.keys(resp.headers || {}).find(key => key.toLowerCase() === "x-api-status-code");
  if (!header) return "";
  const value = resp.headers[header];
  return String(Array.isArray(value) ? value[0] : value);
}

export function extractUsage(ctx) {
  if (ctx.model === TTS) return { kilochars: Array.from(ctx.requestBody.text).length / 1000 };
  return { hours: MAX_ASR_HOURS };
}

export function parseSubmitResponse(ctx, resp) {
  if (ctx.model === ASR) {
    if (asrCode(resp) !== "20000000") throw new Error("Doubao did not accept the transcription");
    const upstreamId = resp.body && typeof resp.body.task_id === "string" ? resp.body.task_id : ctx.requestBody.requestId;
    return { taskId: upstreamId };
  }
  if (typeof resp.body !== "string") throw new Error("invalid Doubao speech response");
  const chunks = [];
  let characters;
  for (const line of resp.body.split(/\r?\n/)) {
    if (!line.trim()) continue;
    const part = JSON.parse(line);
    if (part.code === 20000000) {
      characters = part.usage && part.usage.text_words;
      break;
    }
    if (part.code !== 0) throw new Error("Doubao speech synthesis failed");
    if (typeof part.data === "string" && part.data) chunks.push(part.data);
  }
  if (!chunks.length || !Number.isInteger(characters) || characters < 0 || characters > MAX_CHARACTERS) {
    throw new Error("Doubao did not return audio and valid character usage");
  }
  return {
    taskId: ctx.publicTaskId,
    taskData: { audio_chunks: chunks, mime_type: "audio/mpeg", sample_rate: 24000, characters },
    immediate: { status: "SUCCESS", progress: "100%" },
  };
}

export function buildQueryRequest(ctx) {
  return {
    url: ctx.baseUrl + "/api/v3/auc/bigmodel/query",
    method: "POST",
    headers: headers(ctx, ASR, ctx.taskId),
    body: {},
  };
}

export function parseTaskResult(_ctx, body, resp) {
  const code = asrCode(resp);
  if (code === "20000001" || code === "20000002") return { status: "IN_PROGRESS", progress: "50%" };
  if (code === "20000000" && body && body.result && typeof body.result.text === "string" && validDuration(body)) {
    return { status: "SUCCESS", progress: "100%" };
  }
  // Doubao returns 20000003 for a recording with no detected speech.
  if (code.startsWith("45") || code === "20000000" || code === "20000003") {
    return { status: "FAILURE", progress: "100%", reason: "Doubao transcription failed or returned invalid usage" };
  }
  return { status: "UNKNOWN" };
}

function validDuration(body) {
  const ms = body && body.audio_info && body.audio_info.duration;
  return Number.isFinite(ms) && ms > 0 && ms <= MAX_ASR_HOURS * 3600000;
}

export function extractUsageOnComplete(ctx, result, body) {
  if (result.status !== "SUCCESS") return {};
  if (ctx.model === TTS) return { kilochars: body.characters / 1000 };
  if (validDuration(body)) return { hours: body.audio_info.duration / 3600000 };
  return {};
}
