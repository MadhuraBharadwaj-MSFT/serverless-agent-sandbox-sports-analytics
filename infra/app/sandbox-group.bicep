@description('Name of the Container Apps sandbox group. This is the ARM resource that scopes individual sandboxes and the data-plane RBAC.')
param sandboxGroupName string

@description('Location for the sandbox group. Must be a region where Microsoft.App/sandboxGroups is available (e.g. eastus2, westus2, westus3, swedencentral). NOT eastus.')
param location string

@description('Resource tags.')
param tags object = {}

// Container Apps sandbox group (preview).
// The group is an empty container: an individual sandbox is created at runtime
// on the data plane (management.<region>.azuredevcompute.io) by the function
// app's managed identity once it has the SandboxGroup Data Owner role. This app
// keeps ONE sandbox warm across timer runs, so the data stack is installed once
// and the loaded DataFrame survives between analysis steps.
resource sandboxGroup 'Microsoft.App/sandboxGroups@2026-02-01-preview' = {
  name: sandboxGroupName
  location: location
  tags: tags
}

output sandboxGroupName string = sandboxGroup.name
output sandboxGroupId string = sandboxGroup.id
