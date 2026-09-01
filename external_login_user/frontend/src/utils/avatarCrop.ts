export function cropRect(width: number, height: number, zoom: number, x: number, y: number) {
  const safeZoom = Math.max(1, Math.min(4, zoom));
  const side = Math.min(width, height) / safeZoom;
  const clamp = (value:number) => Math.max(-1, Math.min(1,value));
  return { x:(width-side)*(clamp(x)+1)/2, y:(height-side)*(clamp(y)+1)/2, size:side };
}
