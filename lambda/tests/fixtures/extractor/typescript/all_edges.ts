import { helper } from './helpers';
import * as logger from 'pino';
import lodash from 'lodash';

interface IService {
  run(): void;
}

class BaseService {
  log(): void {
    logger.info('base');
  }
}

export class Service extends BaseService implements IService {
  run(): void {
    helper();
    this.log();
    lodash.map([], () => null);
  }
}

export function topLevel(): void {
  new Service().run();
}

export const arrowFn = (x: number) => {
  helper();
  return x;
};
