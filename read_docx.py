import zipfile
import xml.etree.ElementTree as ET
import sys

def docx_to_text(docx_path):
    try:
        with zipfile.ZipFile(docx_path) as z:
            xml_content = z.read('word/document.xml')
            root = ET.fromstring(xml_content)
            
            # The namespace for Word XML elements
            namespaces = {'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
            
            # Find all text elements
            text_runs = []
            for paragraph in root.findall('.//w:p', namespaces):
                p_text = []
                for text in paragraph.findall('.//w:t', namespaces):
                    if text.text:
                        p_text.append(text.text)
                text_runs.append(''.join(p_text))
            
            return '\n'.join(text_runs)
    except Exception as e:
        return f"Error: {e}"

if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("Usage: python read_docx.py <path_to_docx>")
        sys.exit(1)
    
    content = docx_to_text(sys.argv[1])
    print(content)
