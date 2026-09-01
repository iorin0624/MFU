import { describe, expect, it } from 'vitest';
import { composerHeight } from './chatComposer';

describe('chat composer automatic height', () => {
  it('grows for newline or wrapped content and shrinks back to one line', () => {
    expect(composerHeight(66,22,22,true,600)).toEqual({height:66,overflow:'hidden'});
    expect(composerHeight(44,22,22,true,600)).toEqual({height:44,overflow:'hidden'});
  });
  it('caps mobile at four lines and scrolls additional content', () => {
    expect(composerHeight(110,22,22,true,600)).toEqual({height:110,overflow:'hidden'});
    expect(composerHeight(132,22,22,true,600)).toEqual({height:110,overflow:'auto'});
  });
  it('caps desktop at twenty lines', () => {
    expect(composerHeight(462,22,22,false,1000)).toEqual({height:462,overflow:'hidden'});
    expect(composerHeight(484,22,22,false,1000)).toEqual({height:462,overflow:'auto'});
  });
  it('prioritizes staying visible when the keyboard leaves little space', () => {
    expect(composerHeight(132,22,22,true,80)).toEqual({height:80,overflow:'auto'});
    expect(composerHeight(132,22,22,true,20)).toEqual({height:44,overflow:'auto'});
  });
});
