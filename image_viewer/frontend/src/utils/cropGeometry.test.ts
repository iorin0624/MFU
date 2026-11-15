import { describe, expect, it } from 'vitest';
import {
  createCropRectangle, fitCropRatio, moveCropRectangle, resizeCropRectangle,
  type CropRectangle,
} from './cropGeometry';

const imageWidth = 1200;
const imageHeight = 900;
const starting: CropRectangle = {x: .2, y: .2, width: .6, height: .6};

function expectInside(rectangle: CropRectangle) {
  expect(rectangle.x).toBeGreaterThanOrEqual(0);
  expect(rectangle.y).toBeGreaterThanOrEqual(0);
  expect(rectangle.x + rectangle.width).toBeLessThanOrEqual(1.000001);
  expect(rectangle.y + rectangle.height).toBeLessThanOrEqual(1.000001);
}

describe('crop selection geometry', () => {
  it('moves an existing selection without changing its size and stops at image edges', () => {
    const moved = moveCropRectangle(starting, 3000, -1000, imageWidth, imageHeight);
    expect(moved).toEqual({x: .4, y: 0, width: .6, height: .6});
  });

  it('resizes free edges and corners while keeping opposite edges fixed', () => {
    const north = resizeCropRectangle(starting, 'n', 0, -90, null, imageWidth, imageHeight);
    expect(north.y).toBeCloseTo(.1);
    expect(north.y + north.height).toBeCloseTo(.8);
    const southeast = resizeCropRectangle(starting, 'se', 120, 90, null, imageWidth, imageHeight);
    expect(southeast.x).toBeCloseTo(.2);
    expect(southeast.y).toBeCloseTo(.2);
    expect(southeast.width).toBeCloseTo(.7);
    expect(southeast.height).toBeCloseTo(.7);
  });

  it('maintains a fixed ratio when a side or corner is dragged', () => {
    const ratio = 4 / 3;
    const fitted = fitCropRatio(starting, ratio, imageWidth, imageHeight);
    const east = resizeCropRectangle(fitted, 'e', -100, 0, ratio, imageWidth, imageHeight);
    const northwest = resizeCropRectangle(fitted, 'nw', -80, -60, ratio, imageWidth, imageHeight);
    for (const result of [fitted, east, northwest]) {
      expect(result.width * imageWidth / (result.height * imageHeight)).toBeCloseTo(ratio);
      expectInside(result);
    }
    expect(east.x).toBeCloseTo(fitted.x);
    expect(northwest.x + northwest.width).toBeCloseTo(fitted.x + fitted.width);
    expect(northwest.y + northwest.height).toBeCloseTo(fitted.y + fitted.height);
  });

  it('keeps the center when switching from landscape to portrait ratio', () => {
    const landscape = fitCropRatio(starting, 3 / 2, imageWidth, imageHeight);
    const portrait = fitCropRatio(landscape, 2 / 3, imageWidth, imageHeight);
    expect(portrait.x + portrait.width / 2).toBeCloseTo(landscape.x + landscape.width / 2);
    expect(portrait.y + portrait.height / 2).toBeCloseTo(landscape.y + landscape.height / 2);
    expect(portrait.width * imageWidth / (portrait.height * imageHeight)).toBeCloseTo(2 / 3);
    expectInside(portrait);
  });

  it('creates a portrait-ratio selection from a new drag', () => {
    const rectangle = createCropRectangle({x: .3, y: .2}, {x: .6, y: .7}, 3 / 4, imageWidth, imageHeight);
    expect(rectangle.width * imageWidth / (rectangle.height * imageHeight)).toBeCloseTo(3 / 4);
    expect(rectangle.x).toBeCloseTo(.3);
    expect(rectangle.y).toBeCloseTo(.2);
    expectInside(rectangle);
  });

  it('clamps side handles to the image and keeps a usable crop', () => {
    const fitted = fitCropRatio(starting, 4 / 3, imageWidth, imageHeight);
    const expanded = resizeCropRectangle(fitted, 'e', 5000, 0, 4 / 3, imageWidth, imageHeight);
    const shrunk = resizeCropRectangle(fitted, 'nw', 5000, 5000, 4 / 3, imageWidth, imageHeight);
    expectInside(expanded);
    expectInside(shrunk);
    expect(expanded.x + expanded.width).toBeCloseTo(1);
    expect(shrunk.width * imageWidth).toBeGreaterThanOrEqual(2);
    expect(shrunk.height * imageHeight).toBeGreaterThanOrEqual(2);
  });
});
