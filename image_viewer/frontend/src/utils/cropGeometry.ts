export type CropRectangle = {x: number; y: number; width: number; height: number};
export type CropPoint = {x: number; y: number};
export type CropHandle = 'n' | 'ne' | 'e' | 'se' | 's' | 'sw' | 'w' | 'nw';

const clamp = (value: number, minimum: number, maximum: number) =>
  Math.min(Math.max(value, minimum), maximum);

function pixels(rectangle: CropRectangle, imageWidth: number, imageHeight: number) {
  const left = rectangle.x * imageWidth;
  const top = rectangle.y * imageHeight;
  return {
    left, top,
    right: left + rectangle.width * imageWidth,
    bottom: top + rectangle.height * imageHeight,
  };
}

function rectangleFromPixels(
  left: number, top: number, right: number, bottom: number,
  imageWidth: number, imageHeight: number,
): CropRectangle {
  return {
    x: left / imageWidth, y: top / imageHeight,
    width: (right - left) / imageWidth, height: (bottom - top) / imageHeight,
  };
}

export function fitCropRatio(
  rectangle: CropRectangle, ratio: number, imageWidth: number, imageHeight: number,
): CropRectangle {
  if (!Number.isFinite(ratio) || ratio <= 0) return rectangle;
  const centerX = (rectangle.x + rectangle.width / 2) * imageWidth;
  const centerY = (rectangle.y + rectangle.height / 2) * imageHeight;
  const maximumWidth = Math.min(
    2 * Math.min(centerX, imageWidth - centerX),
    2 * Math.min(centerY, imageHeight - centerY) * ratio,
  );
  const width = Math.min(
    maximumWidth,
    Math.max(rectangle.width * imageWidth, rectangle.height * imageHeight * ratio),
  );
  const height = width / ratio;
  return rectangleFromPixels(
    centerX - width / 2, centerY - height / 2,
    centerX + width / 2, centerY + height / 2, imageWidth, imageHeight,
  );
}

export function moveCropRectangle(
  rectangle: CropRectangle, deltaX: number, deltaY: number,
  imageWidth: number, imageHeight: number,
): CropRectangle {
  return {
    ...rectangle,
    x: clamp(rectangle.x + deltaX / imageWidth, 0, 1 - rectangle.width),
    y: clamp(rectangle.y + deltaY / imageHeight, 0, 1 - rectangle.height),
  };
}

export function createCropRectangle(
  start: CropPoint, end: CropPoint, ratio: number | null,
  imageWidth: number, imageHeight: number,
): CropRectangle {
  let deltaX = (end.x - start.x) * imageWidth;
  let deltaY = (end.y - start.y) * imageHeight;
  if (ratio !== null) {
    const signX = deltaX < 0 ? -1 : 1;
    const signY = deltaY < 0 ? -1 : 1;
    const anchorX = start.x * imageWidth;
    const anchorY = start.y * imageHeight;
    const maxWidth = Math.min(
      signX > 0 ? imageWidth - anchorX : anchorX,
      (signY > 0 ? imageHeight - anchorY : anchorY) * ratio,
    );
    const width = Math.min(Math.max(Math.abs(deltaX), Math.abs(deltaY) * ratio), maxWidth);
    deltaX = signX * width;
    deltaY = signY * width / ratio;
  }
  const left = Math.min(start.x * imageWidth, start.x * imageWidth + deltaX);
  const top = Math.min(start.y * imageHeight, start.y * imageHeight + deltaY);
  return rectangleFromPixels(left, top, left + Math.abs(deltaX), top + Math.abs(deltaY), imageWidth, imageHeight);
}

export function resizeCropRectangle(
  rectangle: CropRectangle, handle: CropHandle, deltaX: number, deltaY: number,
  ratio: number | null, imageWidth: number, imageHeight: number,
): CropRectangle {
  const {left, top, right, bottom} = pixels(rectangle, imageWidth, imageHeight);
  const east = handle.includes('e');
  const west = handle.includes('w');
  const north = handle.includes('n');
  const south = handle.includes('s');
  if (ratio === null) {
    return rectangleFromPixels(
      west ? clamp(left + deltaX, 0, right - 2) : left,
      north ? clamp(top + deltaY, 0, bottom - 2) : top,
      east ? clamp(right + deltaX, left + 2, imageWidth) : right,
      south ? clamp(bottom + deltaY, top + 2, imageHeight) : bottom,
      imageWidth, imageHeight,
    );
  }

  const minimumWidth = Math.max(2, 2 * ratio);
  if ((east || west) && (north || south)) {
    const anchorX = east ? left : right;
    const anchorY = south ? top : bottom;
    const horizontalChange = (east ? 1 : -1) * deltaX;
    const verticalChange = (south ? 1 : -1) * deltaY * ratio;
    const change = Math.abs(horizontalChange) >= Math.abs(verticalChange)
      ? horizontalChange : verticalChange;
    const maximumWidth = Math.min(
      east ? imageWidth - anchorX : anchorX,
      (south ? imageHeight - anchorY : anchorY) * ratio,
    );
    const width = clamp(right - left + change, Math.min(minimumWidth, maximumWidth), maximumWidth);
    const height = width / ratio;
    return rectangleFromPixels(
      east ? anchorX : anchorX - width,
      south ? anchorY : anchorY - height,
      east ? anchorX + width : anchorX,
      south ? anchorY + height : anchorY,
      imageWidth, imageHeight,
    );
  }

  if (east || west) {
    const anchorX = east ? left : right;
    const centerY = (top + bottom) / 2;
    const maximumWidth = Math.min(
      east ? imageWidth - anchorX : anchorX,
      2 * Math.min(centerY, imageHeight - centerY) * ratio,
    );
    const width = clamp(
      right - left + (east ? deltaX : -deltaX),
      Math.min(minimumWidth, maximumWidth), maximumWidth,
    );
    const height = width / ratio;
    return rectangleFromPixels(
      east ? anchorX : anchorX - width, centerY - height / 2,
      east ? anchorX + width : anchorX, centerY + height / 2,
      imageWidth, imageHeight,
    );
  }

  const anchorY = south ? top : bottom;
  const centerX = (left + right) / 2;
  const maximumHeight = Math.min(
    south ? imageHeight - anchorY : anchorY,
    2 * Math.min(centerX, imageWidth - centerX) / ratio,
  );
  const minimumHeight = Math.max(2, 2 / ratio);
  const height = clamp(
    bottom - top + (south ? deltaY : -deltaY),
    Math.min(minimumHeight, maximumHeight), maximumHeight,
  );
  const width = height * ratio;
  return rectangleFromPixels(
    centerX - width / 2, south ? anchorY : anchorY - height,
    centerX + width / 2, south ? anchorY + height : anchorY,
    imageWidth, imageHeight,
  );
}
