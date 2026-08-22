// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/**
 * DataRegistry stores proof that a data file existed at a certain time.
 * It stores ONLY the file hash and metadata, not the actual file contents.
 */
contract DataRegistry {
    struct Record {
        bytes32 fileHash;       // SHA-256 hash of the file contents
        string fileName;        // local filename or data ID
        string storageURI;      // where the actual file is stored, e.g. local path, S3 URL, IPFS CID
        uint256 fileSizeBytes;  // file size
        string metadataJSON;    // JSON string with extra metadata such as measurement date, sequence, file type
        uint256 createdAt;      // block timestamp
        address submittedBy;    // wallet/account that submitted the record
    }

    Record[] private records;

    event RecordAdded(
        uint256 indexed recordId,
        bytes32 indexed fileHash,
        string fileName,
        string storageURI,
        uint256 fileSizeBytes,
        string metadataJSON,
        uint256 createdAt,
        address indexed submittedBy
    );

    function addRecord(
        bytes32 fileHash,
        string calldata fileName,
        string calldata storageURI,
        uint256 fileSizeBytes,
        string calldata metadataJSON
    ) external returns (uint256 recordId) {
        records.push(
            Record({
                fileHash: fileHash,
                fileName: fileName,
                storageURI: storageURI,
                fileSizeBytes: fileSizeBytes,
                metadataJSON: metadataJSON,
                createdAt: block.timestamp,
                submittedBy: msg.sender
            })
        );

        recordId = records.length - 1;

        emit RecordAdded(
            recordId,
            fileHash,
            fileName,
            storageURI,
            fileSizeBytes,
            metadataJSON,
            block.timestamp,
            msg.sender
        );
    }

    function getRecord(uint256 recordId) external view returns (Record memory) {
        require(recordId < records.length, "Record does not exist");
        return records[recordId];
    }

    function getRecordCount() external view returns (uint256) {
        return records.length;
    }
}
