// Fixture for the @acme-corp/shared-utils/http2 subpath import case.
// The extractor must preserve the full module specifier so the
// prefix-match query in can resolve every consumer.

import { http2Client } from '@acme-corp/shared-utils/http2';
import { logger } from '@acme-corp/shared-utils/logging';
import defaultExport from '@acme-corp/shared-utils';
import './side-effect-only';

export function ping(): void {
  http2Client();
  logger.info('hello');
  defaultExport();
}
