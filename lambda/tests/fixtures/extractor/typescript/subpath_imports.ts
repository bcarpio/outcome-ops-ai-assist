// Fixture for the @hyatt-hpe/hpe-utils/http2 subpath import case.
// The extractor must preserve the full module specifier so the
// prefix-match query in chunk 04 can resolve every consumer.

import { http2Client } from '@hyatt-hpe/hpe-utils/http2';
import { logger } from '@hyatt-hpe/hpe-utils/logging';
import defaultExport from '@hyatt-hpe/hpe-utils';
import './side-effect-only';

export function ping(): void {
  http2Client();
  logger.info('hello');
  defaultExport();
}
