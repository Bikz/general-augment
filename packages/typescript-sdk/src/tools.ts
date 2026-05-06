import type { JsonObject, OpenAPIRegistrationOptions } from "./types.js";

export async function registerFromOpenAPI(
  specUrl: string,
  options: OpenAPIRegistrationOptions
): Promise<JsonObject> {
  return options.client.registerOpenAPITools(options.projectId, specUrl, {
    includePaths: options.includePaths,
    excludePaths: options.excludePaths,
    targetCount: options.targetCount,
    autoDeploy: options.autoDeploy
  });
}
