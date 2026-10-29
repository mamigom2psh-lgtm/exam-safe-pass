import { BrowserQRCodeReader } from "@zxing/browser";
import {
  ChecksumException,
  FormatException,
  NotFoundException,
} from "@zxing/library";

const reader = new BrowserQRCodeReader();

function decodeOrNull(decode) {
  try {
    return decode()?.getText() ?? null;
  } catch (error) {
    if (
      error instanceof NotFoundException ||
      error instanceof ChecksumException ||
      error instanceof FormatException
    ) {
      return null;
    }
    throw error;
  }
}

window.ExamSafeQrDecoder = Object.freeze({
  decodeCanvas(canvas) {
    return decodeOrNull(() => reader.decodeFromCanvas(canvas));
  },
  async decodeImage(image) {
    try {
      return (await reader.decodeFromImageElement(image)).getText();
    } catch (error) {
      if (
        error instanceof NotFoundException ||
        error instanceof ChecksumException ||
        error instanceof FormatException
      ) {
        return null;
      }
      throw error;
    }
  },
});
