export const TOKENIZER_REVISION = '93b36ff09519291b77d6000d2e86bd8565378086';
export const TOKENIZER_VERSION = `Supabase/gte-small@${TOKENIZER_REVISION};tokenizers.js@0.2.0`;
export const EMBEDDING_PREFIX = 'Archive context, role-labeled historical evidence.\n';

export class InputError extends Error {}

export function validateVector(value) {
  if (!Array.isArray(value) || value.length !== 384 || value.some(x => typeof x !== 'number' || !Number.isFinite(x))) {
    throw new InputError('invalid_embedding');
  }
  const norm = Math.sqrt(value.reduce((s, x) => s + x*x, 0));
  if (norm < 0.99 || norm > 1.01) throw new InputError('embedding_not_normalized');
  return value;
}

// Offsets are Unicode code points, matching PostgreSQL length/substr, not UTF-16.
// Encode without truncation. Every returned input is checked against the actual
// pinned tokenizer including CLS/SEP and the context prefix.
export function embeddingBatch(text, tokenizer, {covered = 0, maxPieces = 8} = {}) {
  const chars = Array.from(text);
  if (!Number.isInteger(covered) || covered < 0 || covered >= chars.length || !Number.isInteger(maxPieces) || maxPieces < 1 || maxPieces > 16) {
    throw new InputError('invalid_embedding_cursor');
  }
  const pieces = [];
  let end = covered;
  while (end < chars.length && pieces.length < maxPieces) {
    const start = Math.max(0, end - Math.min(64, end));
    // Start conservatively, then shrink geometrically. This avoids twelve
    // full tokenizations per piece on the hosted runtime's 2-second CPU budget.
    // It is deliberately not a maximal-length packing algorithm: coverage and
    // actual tokenizer validation take precedence over filling all 512 tokens.
    let best = Math.min(chars.length, start + 1800);
    let input = "", token_count = 0;
    while (best > end) {
      input = EMBEDDING_PREFIX + chars.slice(start,best).join('');
      token_count = tokenizer.encode(input,{add_special_tokens:true}).ids.length;
      if (token_count <= 512) break;
      best = start + Math.floor((best-start) * 0.7);
    }
    if (best <= end) throw new InputError('tokenizer_cannot_advance');
    pieces.push({start_offset:start, end_offset:best, token_count, tokenizer_version:TOKENIZER_VERSION, input});
    end = best;
  }
  return {pieces, complete:end === chars.length, covered:end};
}

const str = (x, max, field, nullable = false) => {
  if (nullable && (x === undefined || x === null)) return null;
  if (typeof x !== 'string' || !x.trim() || x.length > max) throw new InputError(`invalid_${field}`);
  return x;
};
const integer = (x, fallback, min, max, field) => {
  x = x ?? fallback;
  if (!Number.isInteger(x) || x < min || x > max) throw new InputError(`invalid_${field}`);
  return x;
};
const date = (x, field) => {
  if (x === undefined || x === null) return null;
  const match = typeof x === 'string' && x.match(/^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d{1,6})?(Z|[+-]\d{2}:\d{2})$/);
  if (!match || !Number.isFinite(Date.parse(x))) throw new InputError(`invalid_${field}`);
  const [,year,month,day,hour,minute,second,zone] = match;
  const days = new Date(Date.UTC(Number(year),Number(month),0)).getUTCDate();
  if (+year<1 || +month<1 || +month>12 || +day<1 || +day>days || +hour>23 || +minute>59 || +second>59 ||
    (zone!=='Z' && (+zone.slice(1,3)>23 || +zone.slice(4)>59))) throw new InputError(`invalid_${field}`);
  // Keep PostgreSQL microseconds intact; converting to a JS Date and back loses
  // cursor precision and can repeat the final row of a timeline page forever.
  return x;
};

export function validateOperation(input) {
  if (!input || typeof input !== 'object' || Array.isArray(input)) throw new InputError('invalid_request');
  const operation = input.operation;
  if (!['status','search_context','search_text','search_many','open_context','browse_time','embed_next'].includes(operation)) throw new InputError('unknown_operation');
  const allowed = ['operation','generation',...({status:[],embed_next:[],search_many:['queries'],open_context:['source_uri','offset','length'],browse_time:['from','to','after_time','after_id','limit','include_inactive'],search_context:['query','from','to','role','limit','include_inactive'],search_text:['query','from','to','role','limit','include_inactive']}[operation])];
  if (Object.keys(input).some(key=>!allowed.includes(key))) throw new InputError('unsupported_argument');
  const generation = str(input.generation, 120, 'generation', true);
  if (operation === 'status' || operation === 'embed_next') return {operation, generation};
  if (operation === 'search_many') {
    if (!Array.isArray(input.queries) || input.queries.length < 1 || input.queries.length > 4) throw new InputError('invalid_queries');
    return {operation, queries:input.queries.map(q => {
      if (!q || typeof q!=='object' || Array.isArray(q) || 'operation' in q || 'generation' in q) throw new InputError('invalid_query');
      return validateOperation({...q, operation:'search_context', generation});
    })};
  }
  if (operation === 'open_context') {
    const source_uri = str(input.source_uri, 500, 'source_uri');
    if (!/^archive:\/\/conversation\/[^/]+\/(turn\/\d+|message\/[^/]+)$/.test(source_uri)) throw new InputError('invalid_source_uri');
    return {operation, generation, source_uri, offset:integer(input.offset, 0, 0, 100000000, 'offset'), length:integer(input.length, 16000, 1, 20000, 'length')};
  }
  const from = date(input.from, 'from');
  const to = date(input.to, 'to');
  if (from && to && Date.parse(from) > Date.parse(to)) throw new InputError('inverted_dates');
  if (operation === 'browse_time') {
    if (!from || !to) throw new InputError('timeline_bounds_required');
    const after_time = date(input.after_time, 'after_time');
    const after_id = integer(input.after_id, 0, 0, Number.MAX_SAFE_INTEGER, 'after_id');
    if (Boolean(after_time)!==Boolean(after_id)) throw new InputError('incomplete_cursor');
    if (after_time && (Date.parse(after_time)<Date.parse(from) || Date.parse(after_time)>Date.parse(to))) throw new InputError('cursor_outside_range');
    if (input.include_inactive!==undefined && typeof input.include_inactive!=='boolean') throw new InputError('invalid_include_inactive');
    return {operation, generation, from, to, after_time, after_id, include_inactive:input.include_inactive??false, limit:integer(input.limit, 20, 1, 100, 'limit')};
  }
  const role = input.role ?? 'both';
  if (!['user','assistant','both'].includes(role)) throw new InputError('invalid_role');
  if (input.include_inactive !== undefined && typeof input.include_inactive !== 'boolean') throw new InputError('invalid_include_inactive');
  return {operation, generation, query:str(input.query, 2000, 'query'), from, to, role, limit:integer(input.limit, 8, 1, 20, 'limit'), include_inactive:input.include_inactive ?? false};
}

export async function boundedJson(request, limit = 65536) {
  if (!request.body) throw new InputError('empty_body');
  const reader = request.body.getReader();
  const chunks = [];
  let size = 0;
  try {
    while (true) {
      const {value, done} = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > limit) { await reader.cancel(); throw new InputError('body_too_large'); }
      chunks.push(value);
    }
    const bytes = new Uint8Array(size);
    let at = 0;
    for (const c of chunks) { bytes.set(c, at); at += c.length; }
    return JSON.parse(new TextDecoder('utf-8', {fatal:true}).decode(bytes));
  } catch (error) {
    if (error instanceof InputError) throw error;
    throw new InputError('invalid_json');
  } finally { reader.releaseLock(); }
}

/** @param {Record<string, any>} result @param {string|null} semanticError */
export function retrievalHealth(result, semanticError=null) {
  const reasons=[];
  if (semanticError) reasons.push(semanticError);
  if (result.retrieval_mode==='hybrid_rrf' && (!result.status || result.status.build_status!=='indexed' || result.status.embedded_frames<result.status.frames)) reasons.push('semantic_index_incomplete');
  return {...result,semantic_error:semanticError,degraded:reasons.length>0,degraded_reasons:reasons};
}

export const READ_TOOLS = [
  {name:'search_context', description:'Hybrid search of role-labeled historical context. Candidates are not conclusions; open sources. Assistant text is discovery only.', inputSchema:{type:'object', properties:{query:{type:'string',maxLength:2000},role:{enum:['user','assistant','both']},generation:{type:'string'},from:{type:'string'},to:{type:'string'},limit:{type:'integer',minimum:1,maximum:20},include_inactive:{type:'boolean'}},required:['query'],additionalProperties:false}},
  {name:'search_text', description:'Literal text discovery, with explicit authorship and inactive-branch filters.', inputSchema:{type:'object',properties:{query:{type:'string'},role:{enum:['user','assistant','both']},generation:{type:'string'},from:{type:'string'},to:{type:'string'},limit:{type:'integer',minimum:1,maximum:20},include_inactive:{type:'boolean'}},required:['query'],additionalProperties:false}},
  {name:'open_context', description:'Open immutable evidence with exact role/source pointers and explicit Unicode pagination. Continue next_offset before claiming complete coverage.', inputSchema:{type:'object',properties:{source_uri:{type:'string'},generation:{type:'string'},offset:{type:'integer',minimum:0},length:{type:'integer',minimum:1,maximum:20000}},required:['source_uri'],additionalProperties:false}},
  {name:'browse_time', description:'Browse a bounded time range with a stable date/id cursor. Dates require timezones; pages are not exhaustive answers.', inputSchema:{type:'object',properties:{from:{type:'string'},to:{type:'string'},generation:{type:'string'},after_time:{type:'string'},after_id:{type:'integer',minimum:1},include_inactive:{type:'boolean'},limit:{type:'integer',minimum:1,maximum:100}},required:['from','to'],additionalProperties:false}},
  {name:'search_many', description:'Up to four explicit model-generated search formulations. No SQL query planner invents intent or decides truth.', inputSchema:{type:'object',properties:{queries:{type:'array',minItems:1,maxItems:4,items:{type:'object',properties:{query:{type:'string',maxLength:2000},role:{enum:['user','assistant','both']},from:{type:'string'},to:{type:'string'},limit:{type:'integer',minimum:1,maximum:20},include_inactive:{type:'boolean'}},required:['query'],additionalProperties:false}},generation:{type:'string'}},required:['queries'],additionalProperties:false}},
  {name:'archive_status', description:'Archive generation, freshness and embedding coverage. A new index does not mean new source data.', inputSchema:{type:'object',properties:{generation:{type:'string'}},additionalProperties:false}}
];
