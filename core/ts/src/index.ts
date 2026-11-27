import metadataPolicy from './recording/metadata-policy.json';

export * from './recording';
export * from './navigation';
export { metadataPolicy };
export * from './recording/routes';
export {
  DIFF_TOKENS,
  JP_VARIABLES,
  KIND_TOKENS,
  THEME_TOKENS
} from './viewer/tokens';
export type { ThemeToken } from './viewer/tokens';
