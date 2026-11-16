import boto3  # AWS SDK for Python

def whoami():
    session = boto3.Session(profile_name="aws-bedrock-learning")  # use the CLI profile from Step 3c, no keys in code
    sts = session.client("sts")  # client for the identity service
    return sts.get_caller_identity()  # same call as the CLI check, returns a dict

if __name__ == "__main__":  # runs only when executed directly, not on import
    print(whoami())