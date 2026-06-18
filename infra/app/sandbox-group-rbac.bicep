@description('Name of the existing sandbox group to scope the role assignments to.')
param sandboxGroupName string

@description('Principal ID of the function app managed identity (service principal).')
param managedIdentityPrincipalId string

@description('Optional principal ID of the deploying user, so you can drive the data plane locally with the CLI. Leave empty to skip.')
param userPrincipalId string = ''

// Container Apps SandboxGroup Data Owner.
// Grants data-plane access (create/exec/suspend/resume/delete sandboxes)
// on management.<region>.azuredevcompute.io for this sandbox group.
var sandboxGroupDataOwnerRoleId = 'c24cf47c-5077-412d-a19c-45202126392c'

resource sandboxGroup 'Microsoft.App/sandboxGroups@2026-02-01-preview' existing = {
  name: sandboxGroupName
}

resource miSandboxRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(sandboxGroup.id, managedIdentityPrincipalId, sandboxGroupDataOwnerRoleId)
  scope: sandboxGroup
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', sandboxGroupDataOwnerRoleId)
    principalId: managedIdentityPrincipalId
    principalType: 'ServicePrincipal'
  }
}

resource userSandboxRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(userPrincipalId)) {
  name: guid(sandboxGroup.id, userPrincipalId, sandboxGroupDataOwnerRoleId)
  scope: sandboxGroup
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', sandboxGroupDataOwnerRoleId)
    principalId: userPrincipalId
    principalType: 'User'
  }
}
