import type { JsonObject, LinkUserOptions } from "./types.js";
import type { GeneralAugmentClient } from "./client.js";

export async function linkUser(
  client: GeneralAugmentClient,
  projectId: string,
  options: LinkUserOptions
): Promise<JsonObject> {
  return client.linkUser(projectId, options);
}

export async function resolveUser(
  client: GeneralAugmentClient,
  projectId: string,
  phone: string
): Promise<JsonObject> {
  return client.resolveUser(projectId, phone);
}

export async function unlinkUser(
  client: GeneralAugmentClient,
  projectId: string,
  phone: string
): Promise<JsonObject> {
  return client.unlinkUser(projectId, phone);
}
