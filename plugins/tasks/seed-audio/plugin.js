const MODEL = "seed-audio-1.0";
const MAX_SECONDS = 120;

export const meta = {
  apiVersion: 1,
  key: "seed-audio",
  name: "Doubao Seed Audio",
  description: {
    en: "Generate audio from a prompt and optional reference media",
    zh: "通过提示词和可选的参考素材生成音频",
  },
  version: "1.0.0",
  author: { name: "PackAPI" },
  icon: "Doubao.Color",
  baseUrl: "https://openspeech.bytedance.com",
  models: [MODEL],
  fetchMode: "per_task",
  usageSchema: {
    seconds: {
      type: "number",
      unit: "second",
      description: { en: "Audio generation unit price", zh: "音频生成单价" },
    },
  },
  usageExamples: [{ label: "10s", facts: { seconds: 10 } }],
  routes: [{
    method: "POST",
    path: "/doubao/api/v3/tts/create",
    type: "submit",
    decode: "createAudio",
    render: "audioCreated",
    models: [MODEL],
    retainResult: false,
  }],
};

export const native = {
  createAudio(ctx) {
    const body = ctx.body.kind === "json" ? ctx.body.value : null;
    if (!body || typeof body !== "object" || Array.isArray(body)) throw new Error("JSON object required");
    if (body.model !== MODEL) throw new Error("model must be seed-audio-1.0");
    const prompt = body.text_prompt;
    if (typeof prompt !== "string" || !prompt.trim() || Array.from(prompt).length > 3000) {
      throw new Error("text_prompt must contain 1 to 3000 characters");
    }
    return { kind: "submit", model: MODEL, action: "generate_audio", requestBody: body };
  },
  audioCreated(_ctx, task) {
    return task.data;
  },
  error(_ctx, error) {
    return { error: { code: error.code, message: error.message } };
  },
};

export function buildSubmitRequest(ctx) {
  return {
    url: ctx.baseUrl + "/api/v3/tts/create",
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "Accept": "application/json",
      "X-Api-Key": ctx.apiKey,
      "X-Api-Request-Id": utils.uuid(),
    },
    body: ctx.requestBody,
  };
}

export function extractUsage() {
  return { seconds: MAX_SECONDS };
}

export function parseSubmitResponse(ctx, resp) {
  const body = resp.body;
  if (!body || typeof body !== "object" || Array.isArray(body)) throw new Error("invalid Seed Audio response");
  if (typeof body.url !== "string" || !body.url.startsWith("https://")) {
    throw new Error("Seed Audio did not return an HTTPS audio URL");
  }
  if (!Number.isFinite(body.original_duration) || body.original_duration <= 0 || body.original_duration > MAX_SECONDS) {
    throw new Error("Seed Audio returned an invalid original_duration");
  }
  // The signed URL serves the same audio without returning the large inline copy.
  const taskData = { url: body.url, original_duration: body.original_duration };
  if (Number.isFinite(body.duration) && body.duration > 0) taskData.duration = body.duration;
  if (body.subtitle !== undefined) taskData.subtitle = body.subtitle;
  return {
    taskId: ctx.publicTaskId,
    taskData,
    immediate: { status: "SUCCESS", progress: "100%" },
  };
}

export function extractUsageOnComplete(_ctx, _result, data) {
  return { seconds: data.original_duration };
}

export function buildQueryRequest() {
  throw new Error("Seed Audio returns its result in the create request");
}

export function parseTaskResult() {
  return { status: "UNKNOWN" };
}
