const fs = require("fs");
const path = require("path");
const { ethers } = require("hardhat");

async function main() {
  const DataRegistry = await ethers.getContractFactory("DataRegistry");
  const registry = await DataRegistry.deploy();
  await registry.waitForDeployment();

  const address = await registry.getAddress();
  console.log(`DataRegistry deployed to: ${address}`);

  const outDir = path.join(__dirname, "..", "artifacts_on_chain");
  fs.mkdirSync(outDir, { recursive: true });
  fs.writeFileSync(
    path.join(outDir, "contract-address.json"),
    JSON.stringify({ DataRegistry: address }, null, 2)
  );
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
