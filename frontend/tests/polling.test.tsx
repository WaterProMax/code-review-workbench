import {renderHook, act, waitFor} from '@testing-library/react';
import {test, expect, vi} from 'vitest';
import {useTaskData} from '../src/hooks/useTaskData';
test('resume refresh restarts polling until completion', async()=>{
  let status='waiting_recovery';
  let reads=0;
  vi.stubGlobal('fetch', vi.fn(async(input)=>{
    const url=String(input);
    let body:any=[];
    if(url==='/api/tasks/T-1') {reads++; body={status};}
    else if(url.includes('/events')) body={events:[],next_seq:0};
    return new Response(JSON.stringify(body),{status:200});
  }));
  const {result,unmount}=renderHook(()=>useTaskData('T-1'));
  await waitFor(()=>expect(result.current.detail?.status).toBe('waiting_recovery'));
  status='running';
  act(()=>result.current.refresh());
  await waitFor(()=>expect(result.current.detail?.status).toBe('running'));
  status='completed';
  await act(async()=>{await new Promise(r=>setTimeout(r,1800));});
  expect(reads).toBeGreaterThanOrEqual(3);
  expect(result.current.detail?.status).toBe('completed');
  unmount(); vi.unstubAllGlobals();
});
