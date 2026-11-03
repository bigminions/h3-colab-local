'use strict';
const assert = require('node:assert/strict');
const {snapshot,formatDuration} = require('../static/job-progress.js');
let passed = 0;
function test(name, run) { run(); passed++; console.log('PASS ' + name); }
const start = Date.parse('2026-10-14T23:59:30+08:00');
function job(overrides = {}) {
  return {id:'first',created:start / 1000,status:'running',message:'正在生成',events:[],...overrides};
}
test('running elapsed includes the time between clicking and server creation', () => {
  const task = job();
  assert.equal(snapshot(task,[],start + 15000,{first:start - 1000}).elapsed,16000);
  assert.equal(snapshot(task,[],start + 16000,{first:start - 1000}).elapsed,17000);
});
test('completed duration freezes at the final event, even after a reload', () => {
  const task = job({status:'done',events:[{timestamp:(start + 70000) / 1000,message:'已完成'}]});
  assert.deepEqual(snapshot(task,[],start + 100000),snapshot(task,[],start + 200000));
  assert.equal(snapshot(task).elapsed,70000);
  assert.equal(snapshot(task).running,false);
});
test('timestamps keep durations accurate across midnight', () => {
  const task = job({status:'done',events:[{timestamp:(start + 10000) / 1000,message:'开始'},
    {timestamp:(start + 100000) / 1000,message:'准备完成\n视频已保存'}]});
  const result = snapshot(task);
  assert.equal(result.elapsed,100000);
  assert.equal(result.message,'视频已保存');
});

test('selecting an earlier batch result still shows the current batch runtime', () => {
  const first = job({batch_id:'batch',status:'done',events:[{timestamp:(start + 20000) / 1000,message:'第一条已保存'}]});
  const second = job({id:'second',batch_id:'batch',created:(start + 1) / 1000,message:'正在生成第二条'});
  const result = snapshot(first,[first,second],start + 50000,{first:start - 1000},'second');
  assert.equal(result.running,true);
  assert.equal(result.elapsed,51000);
  assert.equal(result.message,'正在生成第二条');
  second.status = 'done'; second.events = [{timestamp:(start + 90000) / 1000,message:'整批完成'}];
  assert.equal(snapshot(first,[first,second],start + 200000).elapsed,90000);
});
test('failed tasks freeze and tasks without timestamps show missing duration', () => {
  const failed = job({status:'error',events:[{timestamp:(start + 8000) / 1000,message:'连接失败'}]});
  assert.equal(snapshot(failed,[],start + 20000).elapsed,8000);
  assert.equal(snapshot(job({status:'done'})).elapsed,null);
  assert.equal(snapshot(null).message,'等待开始');
});
test('duration formatting handles hours and invalid values', () => {
  assert.equal(formatDuration(0),'00:00');
  assert.equal(formatDuration(65000),'01:05');
  assert.equal(formatDuration(3661000),'01:01:01');
  assert.equal(formatDuration(null),'—');
});
console.log(`${passed} progress checks passed`);
