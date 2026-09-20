import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {createHash} from 'node:crypto';
import {Tokenizer} from '@huggingface/tokenizers';
import {embeddingBatch,EMBEDDING_PREFIX} from '../../supabase/functions/crowley-archive-vnext/core.mjs';
const base=new URL('../../supabase/functions/crowley-archive-vnext/vendor/',import.meta.url);
const raw=readFileSync(new URL('tokenizer.json',base));
const config=readFileSync(new URL('tokenizer_config.json',base));
const tokenizer=new Tokenizer(JSON.parse(raw),JSON.parse(config));
test('vendored tokenizer is the pinned model artifact',()=>{
 assert.equal(createHash('sha256').update(raw).digest('hex'),'da0e79933b9ed51798a3ae27893d3c5fa4a201126cef75586296df9b4d2c62a0');
 assert.equal(createHash('sha256').update(config).digest('hex'),'73687f47b47aedc8bfa8712f7e6616450058f1f1bb3d5e5861f8a92964d6467a');
});
test('real tokenizer covers long prose, code and multilingual text without truncation',()=>{
 for(const body of ['Ordinary synthetic historical evidence. '.repeat(1200),'🦊 中文 日本語 café é — 𠮷 '.repeat(1200),'const x = {value: "abc_xyz_0123"}; /* [] */\n'.repeat(1200)]){
  const chars=Array.from(body);let end=0;let batches=0;
  while(end<chars.length){
   const x=embeddingBatch(body,tokenizer,{covered:end,maxPieces:2});
   assert.ok(x.covered>end);
   for(const p of x.pieces){
    assert.ok(p.start_offset<=end&&p.end_offset>end);
    assert.equal(p.input,EMBEDDING_PREFIX+chars.slice(p.start_offset,p.end_offset).join(''));
    assert.equal(p.token_count,tokenizer.encode(p.input,{add_special_tokens:true}).ids.length);
    assert.ok(p.token_count<=512);end=p.end_offset;
   }
   assert.equal(x.complete,end===chars.length);batches++;
   assert.ok(batches<1000);
  }
 }
});
