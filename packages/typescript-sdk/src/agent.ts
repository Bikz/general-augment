import type { AgentTestResponse } from "./types.js";
import type { GeneralAugmentClient } from "./client.js";

export class AgentClient {
  private client: GeneralAugmentClient;
  private projectId: string;

  constructor(client: GeneralAugmentClient, projectId: string) {
    this.client = client;
    this.projectId = projectId;
  }

  async test(
    message: string,
    options: { phoneE164?: string; channel?: string } = {}
  ): Promise<AgentTestResponse> {
    return this.client.testAgent(this.projectId, message, options);
  }
}

export async function testAgent(
  client: GeneralAugmentClient,
  projectId: string,
  message: string,
  options: { phoneE164?: string; channel?: string } = {}
): Promise<AgentTestResponse> {
  return client.testAgent(projectId, message, options);
}
