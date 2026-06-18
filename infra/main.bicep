targetScope = 'subscription'

@minLength(1)
@maxLength(64)
@description('Name of the environment used to generate a short unique hash for resource names.')
param environmentName string

@minLength(1)
@description('Primary location for all resources. Must support Azure Functions Flex Consumption, Microsoft.App/sandboxGroups (preview), and the default Microsoft Foundry gpt-4.1 Global Standard deployment. NOTE: eastus does NOT support sandboxGroups.')
@allowed([
  'eastus2'
  'westus2'
  'westus3'
  'centralus'
  'northcentralus'
  'westcentralus'
  'swedencentral'
  'uksouth'
  'northeurope'
  'australiaeast'
])
@metadata({
  azd: {
    type: 'location'
  }
})
param location string

@description('Name of the Blob container the agent reads the input CSV from each run.')
param inputContainerName string = 'input'

@description('Name of the Blob container the agent writes the HTML dashboard and results to.')
param outputContainerName string = 'output'

@description('Fixed blob name analysed each run (drop a CSV with this name into the input container).')
param inputBlobName string = 'matches.csv'

@description('Microsoft Foundry model deployment name.')
param foundryModel string = 'gpt-4.1'

@description('Microsoft Foundry model name.')
param foundryModelName string = 'gpt-4.1'

@description('Microsoft Foundry model version.')
param foundryModelVersion string = '2025-04-14'

@description('Microsoft Foundry deployment capacity (thousands of TPM; RPM scales 1:1). 500 gives burst headroom so multi-step agent runs do not hit HTTP 429.')
param foundryDeploymentCapacity int = 500

@description('Optional reasoning effort for supported Foundry reasoning models. Leave empty for older models such as gpt-4.1, which do not support reasoning settings.')
@allowed([
  ''
  'none'
  'low'
  'medium'
  'high'
  'xhigh'
])
param reasoningEffort string = ''

@description('Reasoning summary mode for supported Foundry reasoning models. Only used when reasoningEffort is set.')
@allowed([
  ''
  'auto'
  'concise'
  'detailed'
])
param reasoningSummary string = 'concise'

var abbrs = loadJsonContent('./abbreviations.json')
var resourceToken = toLower(uniqueString(subscription().id, environmentName, location))
var tags = { 'azd-env-name': environmentName }
var functionAppName = '${abbrs.webSitesFunctions}sportsia-${resourceToken}'
var foundryAccountName = 'cog-${resourceToken}'
var foundryProjectName = '${foundryAccountName}-proj'
var deploymentStorageContainerName = 'app-package-${take(functionAppName, 32)}-${take(toLower(uniqueString(functionAppName, resourceToken)), 7)}'
var deployerPrincipalId = deployer().objectId
var sandboxGroupName = 'sbg${resourceToken}'
var storageAccountName = '${abbrs.storageStorageAccounts}${resourceToken}'
var dataBlobEndpoint = 'https://${storageAccountName}.blob.${environment().suffixes.storage}'
var reasoningAppSettings = !empty(reasoningEffort) ? {
  AZURE_FUNCTIONS_AGENTS_REASONING_EFFORT: reasoningEffort
  AZURE_FUNCTIONS_AGENTS_REASONING_SUMMARY: empty(reasoningSummary) ? 'concise' : reasoningSummary
} : {}

resource rg 'Microsoft.Resources/resourceGroups@2021-04-01' = {
  name: '${abbrs.resourcesResourceGroups}${environmentName}'
  location: location
  tags: tags
}

module apiUserAssignedIdentity 'br/public:avm/res/managed-identity/user-assigned-identity:0.4.1' = {
  name: 'apiUserAssignedIdentity'
  scope: rg
  params: {
    location: location
    tags: tags
    name: '${abbrs.managedIdentityUserAssignedIdentities}sportsia-${resourceToken}'
  }
}

module foundry './app/foundry.bicep' = {
  name: 'foundry'
  scope: rg
  params: {
    accountName: foundryAccountName
    projectName: foundryProjectName
    location: location
    tags: tags
    modelDeploymentName: foundryModel
    modelName: foundryModelName
    modelVersion: foundryModelVersion
    deploymentCapacity: foundryDeploymentCapacity
    managedIdentityPrincipalId: apiUserAssignedIdentity.outputs.principalId
    deployerPrincipalId: deployerPrincipalId
  }
}

module appServicePlan 'br/public:avm/res/web/serverfarm:0.1.1' = {
  name: 'appserviceplan'
  scope: rg
  params: {
    name: '${abbrs.webServerFarms}${resourceToken}'
    sku: {
      name: 'FC1'
      tier: 'FlexConsumption'
    }
    reserved: true
    location: location
    tags: tags
  }
}

module api './app/api.bicep' = {
  name: 'api'
  scope: rg
  params: {
    name: functionAppName
    location: location
    tags: tags
    applicationInsightsName: monitoring.outputs.name
    appServicePlanId: appServicePlan.outputs.resourceId
    runtimeName: 'python'
    runtimeVersion: '3.13'
    storageAccountName: storage.outputs.name
    deploymentStorageContainerName: deploymentStorageContainerName
    identityId: apiUserAssignedIdentity.outputs.resourceId
    identityClientId: apiUserAssignedIdentity.outputs.clientId
    appSettings: union({
      AZURE_FUNCTIONS_AGENTS_PROVIDER: 'foundry'
      FOUNDRY_PROJECT_ENDPOINT: foundry.outputs.projectEndpoint
      FOUNDRY_MODEL: foundry.outputs.modelDeploymentName
      AZURE_CLIENT_ID: apiUserAssignedIdentity.outputs.clientId
      // Persistent Container Apps sandbox wiring for the analysis tool. ONE
      // warm sandbox is reused across timer runs (the data stack is installed
      // once and the loaded DataFrame survives between analysis steps).
      SANDBOX_REGION: location
      SANDBOX_SUBSCRIPTION_ID: subscription().subscriptionId
      SANDBOX_RESOURCE_GROUP: rg.name
      SANDBOX_GROUP_NAME: sandboxGroupName
      // Blob Storage data plane: input CSV in, HTML dashboard + results out.
      DATA_BLOB_ENDPOINT: dataBlobEndpoint
      DATA_INPUT_CONTAINER: inputContainerName
      DATA_OUTPUT_CONTAINER: outputContainerName
      INPUT_BLOB_NAME: inputBlobName
      ENABLE_MULTIPLATFORM_BUILD: 'true'
    }, reasoningAppSettings)
  }
}

module storage 'br/public:avm/res/storage/storage-account:0.8.3' = {
  name: 'storage'
  scope: rg
  params: {
    name: storageAccountName
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false
    dnsEndpointType: 'Standard'
    publicNetworkAccess: 'Enabled'
    networkAcls: {
      defaultAction: 'Allow'
      bypass: 'AzureServices'
    }
    blobServices: {
      containers: [
        { name: deploymentStorageContainerName }
        { name: inputContainerName }
        { name: outputContainerName }
      ]
    }
    minimumTlsVersion: 'TLS1_2'
    location: location
    tags: tags
  }
}

module rbac './app/rbac.bicep' = {
  name: 'rbacAssignments'
  scope: rg
  params: {
    storageAccountName: storage.outputs.name
    appInsightsName: monitoring.outputs.name
    managedIdentityPrincipalId: apiUserAssignedIdentity.outputs.principalId
    deployerPrincipalId: deployerPrincipalId
  }
}

module sandboxGroup './app/sandbox-group.bicep' = {
  name: 'sandboxGroup'
  scope: rg
  params: {
    sandboxGroupName: sandboxGroupName
    location: location
    tags: tags
  }
}

module sandboxGroupRbac './app/sandbox-group-rbac.bicep' = {
  name: 'sandboxGroupRbac'
  scope: rg
  dependsOn: [sandboxGroup]
  params: {
    sandboxGroupName: sandboxGroupName
    managedIdentityPrincipalId: apiUserAssignedIdentity.outputs.principalId
    userPrincipalId: deployerPrincipalId
  }
}

module logAnalytics 'br/public:avm/res/operational-insights/workspace:0.7.0' = {
  name: '${uniqueString(deployment().name, location)}-loganalytics'
  scope: rg
  params: {
    name: '${abbrs.operationalInsightsWorkspaces}${resourceToken}'
    location: location
    tags: tags
    dataRetention: 30
  }
}

module monitoring 'br/public:avm/res/insights/component:0.4.1' = {
  name: '${uniqueString(deployment().name, location)}-appinsights'
  scope: rg
  params: {
    name: '${abbrs.insightsComponents}${resourceToken}'
    location: location
    tags: tags
    workspaceResourceId: logAnalytics.outputs.resourceId
    disableLocalAuth: true
  }
}

output AZURE_LOCATION string = location
output AZURE_FUNCTION_NAME string = api.outputs.SERVICE_API_NAME
output FOUNDRY_PROJECT_ENDPOINT string = foundry.outputs.projectEndpoint
output FOUNDRY_MODEL string = foundry.outputs.modelDeploymentName
output SANDBOX_REGION string = location
output SANDBOX_SUBSCRIPTION_ID string = subscription().subscriptionId
output SANDBOX_RESOURCE_GROUP string = rg.name
output SANDBOX_GROUP_NAME string = sandboxGroupName
output DATA_STORAGE_ACCOUNT string = storage.outputs.name
output DATA_BLOB_ENDPOINT string = dataBlobEndpoint
output DATA_INPUT_CONTAINER string = inputContainerName
output DATA_OUTPUT_CONTAINER string = outputContainerName
output INPUT_BLOB_NAME string = inputBlobName
