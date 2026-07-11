ObjC.import('Vision');
ObjC.import('Foundation');

function run(argv) {
    if (argv.length === 0) {
        return "Error: Missing image path argument.";
    }
    var path = argv[0];
    var url = $.NSURL.fileURLWithPath(path);
    
    // Initialize handler directly with file URL
    var handler = $.VNImageRequestHandler.alloc.initWithURLOptions(url, $.NSDictionary.dictionary);
    if (handler.isNil()) return "Error: Failed to create VNImageRequestHandler";
    
    var request = $.VNRecognizeTextRequest.alloc.init;
    if (request.isNil()) return "Error: Failed to create VNRecognizeTextRequest";
    
    request.recognitionLevel = 0; // VNRequestTextRecognitionLevelAccurate
    
    var error = Ref();
    var requestArray = $.NSArray.arrayWithObject(request);
    var success = handler.performRequestsError(requestArray, error);
    if (!success) {
        return "Error: performRequestsError failed";
    }
    
    var results = request.results;
    var count = results.count;
    var strings = [];
    for (var i = 0; i < count; i++) {
        var obs = results.objectAtIndex(i);
        var text = obs.topCandidates(1).objectAtIndex(0).string;
        strings.push(ObjC.unwrap(text));
    }
    return strings.join("\n");
}
